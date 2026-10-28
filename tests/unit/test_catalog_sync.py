"""D2 catalog lifecycle sync: every product write path dispatches the right op,
and the sync task itself is best-effort (Meta down / missing creds / missing
image never break the local write). The Meta client is always faked."""

import uuid

import pytest

import app.tasks.catalog_sync as catalog_sync
from app.core.catalog_client import CatalogError
from app.core.config import settings
from app.crud import order as crud_order
from app.crud import product as crud_product
from app.db.models import (
    Customer,
    Order,
    OrderStatus,
    PaymentType,
    Product,
    User,
    WhatsAppAccount,
)
from app.schemas.product import ProductCreate, ProductUpdate
from app.services.llm import LLMService, _try_claim_stock
from app.services.merchant_handler import handle_merchant_message

MERCHANT_PHONE = "255700000001"


class FakeCatalogClient:
    """Records calls; behavior knobs per test via instance attributes."""

    instances = []

    def __init__(self, catalog_id, access_token):
        self.catalog_id = catalog_id
        self.access_token = access_token
        self.upserts = []
        self.deletes = []
        self.status_calls = []
        self.handles = ["handle_1"]
        self.status_payload = {
            "data": [
                {
                    "handle": "handle_1",
                    "status": "finished",
                    "errors": [],
                    "errors_total_count": 0,
                }
            ]
        }
        self.fail_with = None
        FakeCatalogClient.instances.append(self)

    async def upsert_items(self, items):
        if self.fail_with:
            raise self.fail_with
        self.upserts.append(items)
        return {"handles": list(self.handles)}

    async def delete_items(self, retailer_ids):
        if self.fail_with:
            raise self.fail_with
        self.deletes.append(retailer_ids)
        return {"handles": list(self.handles)}

    async def get_batch_status(self, handle):
        self.status_calls.append(handle)
        if isinstance(self.status_payload, Exception):
            raise self.status_payload
        return self.status_payload


@pytest.fixture(autouse=True)
def _task_uses_test_session(monkeypatch, db_session):
    monkeypatch.setattr(catalog_sync, "SessionLocal", lambda: db_session)


@pytest.fixture
def catalog_creds(monkeypatch):
    monkeypatch.setattr(settings, "CATALOG_ID", "catalog_123")
    monkeypatch.setattr(settings, "CATALOG_ACCESS_TOKEN", "test-token")


_PRISTINE_FAKE_INIT = FakeCatalogClient.__init__


@pytest.fixture
def fake_client(monkeypatch):
    FakeCatalogClient.instances = []
    FakeCatalogClient.__init__ = _PRISTINE_FAKE_INIT
    monkeypatch.setattr(catalog_sync, "MetaCatalogClient", FakeCatalogClient)
    return FakeCatalogClient


@pytest.fixture
def dispatched(monkeypatch):
    """Records sync_catalog_item.apply_async calls instead of running eager."""
    calls = []

    def fake_apply_async(*args, **kwargs):
        calls.append(kwargs.get("args", args))

    monkeypatch.setattr(catalog_sync.sync_catalog_item, "apply_async", fake_apply_async)
    return calls


@pytest.fixture
def merchant(db_session):
    user = User(username=MERCHANT_PHONE)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def product(db_session, merchant):
    p = Product(
        slug="sync-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=5, merchant_id=merchant.id,
        image_url="https://cdn.example.com/shoes.jpg",
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def merchant_account(db_session):
    owner = User(username="255700000099")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    p = Product(
        slug="m-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=5, merchant_id=owner.id,
        image_url="https://cdn.example.com/shoes.jpg",
    )
    db_session.add(p)
    account = WhatsAppAccount(
        name="Sync Merchant", phone_number_id="SYNC_PHONE",
        access_token="tok", user_id=owner.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _op_args(calls):
    return [(args[0], args[1]) for args in calls]


# --- Hooks: CRUD write paths ---


def test_create_product_dispatches_upsert(db_session, merchant, catalog_creds, dispatched):
    created = crud_product.create_product(
        db_session,
        ProductCreate(name="Boots", max_price=60000, min_price=50000, quantity=3),
        merchant.id,
    )

    assert _op_args(dispatched) == [(str(created.id), "upsert")]


def test_update_product_dispatches_upsert(db_session, product, catalog_creds, dispatched):
    crud_product.update_product(db_session, product, ProductUpdate(name="Shoes V2"))

    assert _op_args(dispatched) == [(str(product.id), "upsert")]


def test_update_product_stock_dispatches_upsert(db_session, product, catalog_creds, dispatched):
    crud_product.update_product_stock(db_session, product, quantity=0, is_active=False)

    assert _op_args(dispatched) == [(str(product.id), "upsert")]


def test_delete_product_dispatches_delete(db_session, product, catalog_creds, dispatched):
    product_id = str(product.id)

    crud_product.delete_product(db_session, product)

    assert _op_args(dispatched) == [(product_id, "delete")]


def test_bulk_upsert_dispatches_for_new_and_updated_rows(
    db_session, merchant, catalog_creds, dispatched
):
    existing = Product(
        slug="bulk-old", name="Old", max_price=10000, min_price=8000,
        quantity=2, merchant_id=merchant.id, sku="SKU-1",
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    crud_product.bulk_upsert_products(
        db_session, merchant.id,
        [
            {"sku": "SKU-1", "quantity": 7},
            {"name": "Brand New", "max_price": 20000, "min_price": 15000, "quantity": 4},
        ],
        dry_run=False,
    )

    ops = _op_args(dispatched)
    assert len(ops) == 2
    assert all(op == "upsert" for _, op in ops)
    assert str(existing.id) in [pid for pid, _ in ops]


def test_bulk_upsert_dry_run_dispatches_nothing(db_session, merchant, catalog_creds, dispatched):
    crud_product.bulk_upsert_products(
        db_session, merchant.id,
        [{"name": "Ghost", "max_price": 1, "min_price": 1, "quantity": 1}],
        dry_run=True,
    )

    assert dispatched == []


def test_image_upload_dispatches_upsert(client, auth_user, db_session, catalog_creds, dispatched, tmp_path, monkeypatch):
    # The R2 fallback writes to a relative static/ dir — keep it in tmp.
    monkeypatch.chdir(tmp_path)
    user, headers = auth_user
    created = crud_product.create_product(
        db_session,
        ProductCreate(name="Camera", max_price=90000, min_price=80000, quantity=2),
        user.id,
    )
    dispatched.clear()

    resp = client.post(
        f"/api/v1/products/{created.id}/image",
        files={"file": ("k.jpg", b"fake-bytes", "image/jpeg")},
        headers=headers,
    )

    assert resp.status_code == 200
    assert _op_args(dispatched) == [(str(created.id), "upsert")]


# --- Hooks: merchant slash commands ---


async def test_stock_command_dispatches_upsert(db_session, merchant_account, catalog_creds, dispatched):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/stock Shoes +10", "255700000099"
    )

    assert "New quantity: 15" in reply
    product = db_session.query(Product).filter(Product.slug == "m-shoes").first()
    assert _op_args(dispatched) == [(str(product.id), "upsert")]


async def test_out_of_stock_command_dispatches_upsert(db_session, merchant_account, catalog_creds, dispatched):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/out_of_stock Shoes", "255700000099"
    )

    assert "out of stock" in reply
    product = db_session.query(Product).filter(Product.slug == "m-shoes").first()
    assert product.quantity == 0
    assert _op_args(dispatched) == [(str(product.id), "upsert")]


async def test_price_command_dispatches_upsert(db_session, merchant_account, catalog_creds, dispatched):
    reply = await handle_merchant_message(
        db_session, merchant_account, "/price Shoes max:60000 min:45000", "255700000099"
    )

    assert "Updated prices" in reply
    product = db_session.query(Product).filter(Product.slug == "m-shoes").first()
    assert _op_args(dispatched) == [(str(product.id), "upsert")]


# --- Hooks: deal-close claim and stock restores ---


def test_claim_stock_dispatches_only_when_reaching_zero(
    db_session, product, catalog_creds, dispatched
):
    assert _try_claim_stock(db_session, product) is True  # 5 -> 4
    assert dispatched == []

    product.quantity = 1
    db_session.commit()
    assert _try_claim_stock(db_session, product) is True  # 1 -> 0
    assert _op_args(dispatched) == [(str(product.id), "upsert")]


def test_claim_stock_lost_race_dispatches_nothing(db_session, product, catalog_creds, dispatched):
    product.quantity = 0
    db_session.commit()

    assert _try_claim_stock(db_session, product) is False
    assert dispatched == []


async def test_dispatch_payment_failure_release_dispatches_upsert(
    db_session, product, catalog_creds, dispatched, monkeypatch
):
    from app.services.tunzaa import TunzaaService

    async def boom(self, **kwargs):
        raise RuntimeError("tunzaa down")

    monkeypatch.setattr(TunzaaService, "initiate_payment", boom)
    customer = Customer(phone_number="255711111111")
    db_session.add(customer)
    db_session.commit()
    product.quantity = 0  # post-claim state
    db_session.commit()
    order = Order(
        customer_id=customer.id, product_id=product.id, quantity=1,
        agreed_price=45000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(order)
    db_session.commit()

    service = LLMService(api_key=None)
    note = await service._dispatch_payment(
        db_session, order, product, "255711111111", PaymentType.FULL
    )

    assert "Something went wrong" in note
    assert order.status == OrderStatus.CANCELLED
    db_session.refresh(product)
    assert product.quantity == 1
    assert _op_args(dispatched) == [(str(product.id), "upsert")]


def test_webhook_payment_failure_restore_dispatches_upsert(
    client, db_session, product, catalog_creds, dispatched, monkeypatch, caplog
):
    """QA round 2 (#57/D2): a last-unit claim (synced out-of-stock) followed
    by a Tunzaa FAILED callback must sync the catalog back to in-stock.
    Best-effort like the other restore hooks: broker down or creds unset
    never breaks the local restore."""
    import hashlib
    import hmac
    import json

    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", "test-tunzaa-secret")

    customer = Customer(phone_number="255799999999")
    db_session.add(customer)
    db_session.commit()

    def seed_order():
        order = Order(
            customer_id=customer.id, product_id=product.id, quantity=1,
            agreed_price=45000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)
        return order

    def post_failed(order):
        payload = {
            "status": "FAILED",
            "reference_id": f"order-{order.id}",
            "transaction_id": f"T-{order.id}",
        }
        sig = hmac.new(
            b"test-tunzaa-secret",
            json.dumps(payload, sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
        return client.post(
            "/api/v1/tunzaa/tunzaa-webhook",
            json=payload,
            headers={"X-Signature": sig},
        )

    # Post-claim state: the last unit was taken (and synced out-of-stock).
    product.quantity = 0
    db_session.commit()

    order = seed_order()
    resp = post_failed(order)

    assert resp.status_code == 200
    db_session.refresh(order)
    db_session.refresh(product)
    assert order.status == OrderStatus.FAILED
    assert product.quantity == 1
    assert _op_args(dispatched) == [(str(product.id), "upsert")]

    # Broker down: dispatch fails loudly but the local restore is unaffected.
    def boom(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(catalog_sync.sync_catalog_item, "apply_async", boom)
    order2 = seed_order()
    resp2 = post_failed(order2)

    assert resp2.status_code == 200
    db_session.refresh(order2)
    db_session.refresh(product)
    assert order2.status == OrderStatus.FAILED
    assert product.quantity == 2
    assert "dispatch failed" in caplog.text.lower()

    # Creds unset: silent no-op dispatch, local restore still unaffected.
    monkeypatch.setattr(settings, "CATALOG_ID", None)
    monkeypatch.setattr(settings, "CATALOG_ACCESS_TOKEN", None)
    order3 = seed_order()
    resp3 = post_failed(order3)

    assert resp3.status_code == 200
    db_session.refresh(order3)
    db_session.refresh(product)
    assert order3.status == OrderStatus.FAILED
    assert product.quantity == 3
    assert _op_args(dispatched) == [(str(product.id), "upsert")]


def test_cancel_order_dispatches_upsert(db_session, merchant, product, catalog_creds, dispatched):
    customer = Customer(phone_number="255722222222")
    db_session.add(customer)
    db_session.commit()
    order = Order(
        customer_id=customer.id, product_id=product.id, quantity=1,
        agreed_price=45000, payment_type=PaymentType.FULL, status=OrderStatus.PENDING,
    )
    db_session.add(order)
    db_session.commit()

    crud_order.cancel_order(db_session, order)

    assert _op_args(dispatched) == [(str(product.id), "upsert")]


def test_auto_cancel_order_dispatches_upsert(db_session, merchant, product, catalog_creds, dispatched, monkeypatch):
    import app.tasks.order_reminders as order_reminders

    monkeypatch.setattr(order_reminders, "SessionLocal", lambda: db_session)

    async def fake_send(self, to, body, preview_url=False):
        return {"messages": [{"id": "wamid.x"}]}

    monkeypatch.setattr("app.core.provider.WhatsAppProvider.send_text", fake_send)
    account = WhatsAppAccount(
        name="Cancel Shop", phone_number_id="CANCEL_PHONE",
        access_token="tok", user_id=merchant.id,
    )
    db_session.add(account)
    customer = Customer(phone_number="255733333333")
    db_session.add(customer)
    db_session.commit()
    order = Order(
        customer_id=customer.id, account_id=account.id, product_id=product.id,
        quantity=1, agreed_price=45000, payment_type=PaymentType.FULL,
        status=OrderStatus.PENDING,
    )
    db_session.add(order)
    db_session.commit()
    product_id = str(product.id)

    result = order_reminders.auto_cancel_order(order.id)

    assert result == "Success"
    assert _op_args(dispatched) == [(product_id, "upsert")]


def _seed_platform_product(db_session, slug="taskflow-sub"):
    owner = User(username="255700000997")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    p = Product(
        slug=slug, name="Taskflow OS Subscription", max_price=100000, min_price=100000,
        quantity=999, merchant_id=owner.id, is_subscription=True,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


async def test_pay_now_dispatches_upsert(db_session, catalog_creds, dispatched, monkeypatch):
    from app.services import self_service_billing

    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", "taskflow-sub")
    platform_product = _seed_platform_product(db_session)
    user = User(username="255744444444")
    db_session.add(user)
    db_session.commit()

    # Class-level patch (like the billing task tests): an instance-level
    # patch would leave a shadowing attribute behind after revert.
    async def fake_pay(self, phone, amount, reference):
        return {"transactionID": "TXN1"}

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", fake_pay)

    result = await self_service_billing.pay_now(db_session, user, None)

    assert "order_id" in result
    assert _op_args(dispatched) == [(str(platform_product.id), "upsert")]


async def test_pay_now_release_on_failure_dispatches_again(db_session, catalog_creds, dispatched, monkeypatch):
    from app.services import self_service_billing

    monkeypatch.setattr(settings, "PLATFORM_SUBSCRIPTION_PRODUCT_SLUG", "taskflow-sub")
    platform_product = _seed_platform_product(db_session)
    user = User(username="255755555555")
    db_session.add(user)
    db_session.commit()

    # Class-level patch (like the billing task tests): an instance-level
    # patch would leave a shadowing attribute behind after revert.
    async def boom(self, phone, amount, reference):
        raise RuntimeError("tunzaa down")

    monkeypatch.setattr("app.services.tunzaa.TunzaaService.initiate_payment", boom)

    with pytest.raises(RuntimeError, match="tunzaa down"):
        await self_service_billing.pay_now(db_session, user, None)

    # One dispatch for the decrement, one for the release.
    assert _op_args(dispatched) == [
        (str(platform_product.id), "upsert"),
        (str(platform_product.id), "upsert"),
    ]
    db_session.refresh(platform_product)
    assert platform_product.quantity == 999


# --- Dispatch failure-proofing: creds-unset and broker-down ---


def test_creds_unset_is_silent_noop(db_session, merchant, dispatched, caplog):
    # No catalog_creds fixture: conftest scrubs CATALOG_* to None.
    created = crud_product.create_product(
        db_session,
        ProductCreate(name="Quiet", max_price=1000, min_price=900, quantity=1),
        merchant.id,
    )
    crud_product.update_product(db_session, created, ProductUpdate(quantity=2))
    crud_product.update_product_stock(db_session, created, quantity=0)
    crud_product.delete_product(db_session, created)

    assert dispatched == []
    assert [r for r in caplog.records if r.name == "app.tasks.catalog_sync"] == []


def test_broker_down_does_not_break_write(db_session, merchant, catalog_creds, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(catalog_sync.sync_catalog_item, "apply_async", boom)

    created = crud_product.create_product(
        db_session,
        ProductCreate(name="Sturdy", max_price=1000, min_price=900, quantity=1),
        merchant.id,
    )

    assert db_session.get(Product, created.id) is not None
    assert "dispatch failed" in caplog.text.lower()


# --- Task body: best-effort execution with a mocked client ---


def test_task_upsert_sends_mapped_item_and_polls_status(
    db_session, product, catalog_creds, fake_client
):
    result = catalog_sync.sync_catalog_item(str(product.id), "upsert")

    assert result == "Upserted."
    client = fake_client.instances[0]
    assert client.catalog_id == "catalog_123"
    (items,) = client.upserts
    (item,) = items
    assert item["id"] == str(product.id)
    assert item["title"] == "Shoes"
    assert item["image"] == [{"url": "https://cdn.example.com/shoes.jpg"}]
    # This merchant has no business/account/personal name, so brand falls
    # back to the phone digits (last resort, still valid).
    assert item["brand"] == MERCHANT_PHONE
    assert item["availability"] == "in stock"
    assert item["price"] == "50000.00 TZS"
    assert client.status_calls == ["handle_1"]


def test_task_upsert_zero_quantity_maps_out_of_stock(db_session, product, catalog_creds, fake_client):
    product.quantity = 0
    db_session.commit()

    result = catalog_sync.sync_catalog_item(str(product.id), "upsert")

    assert result == "Upserted."
    (items,) = fake_client.instances[0].upserts
    assert items[0]["availability"] == "out of stock"


def test_task_missing_image_skips_without_client_call(
    db_session, product, catalog_creds, fake_client, caplog
):
    product.image_url = None
    db_session.commit()

    result = catalog_sync.sync_catalog_item(str(product.id), "upsert")

    assert result.startswith("Skipped:")
    assert fake_client.instances[0].upserts == []
    assert "no image" in caplog.text.lower()


def test_task_unusable_merchant_phone_skips(db_session, catalog_creds, fake_client):
    owner = User(username=None, phone_number=None)
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    p = Product(
        slug="no-phone", name="No Phone", max_price=5000, min_price=4000,
        quantity=2, merchant_id=owner.id,
        image_url="https://cdn.example.com/n.jpg",
    )
    db_session.add(p)
    db_session.commit()

    result = catalog_sync.sync_catalog_item(str(p.id), "upsert")

    assert result.startswith("Skipped:")
    assert fake_client.instances[0].upserts == []


def _branded_product(db_session, **user_attrs):
    owner = User(username="255700000050", **user_attrs)
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    p = Product(
        slug=f"branded-{owner.id}", name="Branded", max_price=5000, min_price=4000,
        quantity=2, merchant_id=owner.id,
        image_url="https://cdn.example.com/b.jpg",
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return owner, p


def test_task_brand_prefers_business_name(db_session, catalog_creds, fake_client):
    owner, p = _branded_product(
        db_session, business_name="Kilimanjaro Goods", name="Juma",
    )
    db_session.add(WhatsAppAccount(
        name="Juma's Shop", phone_number_id="BRAND_PHONE_1",
        access_token="tok", user_id=owner.id,
    ))
    db_session.commit()

    result = catalog_sync.sync_catalog_item(str(p.id), "upsert")

    assert result == "Upserted."
    (items,) = fake_client.instances[0].upserts
    assert items[0]["brand"] == "Kilimanjaro Goods"


def test_task_brand_falls_back_to_account_then_personal_name(
    db_session, catalog_creds, fake_client
):
    owner, p = _branded_product(db_session, name="Juma")
    db_session.add(WhatsAppAccount(
        name="Juma's Shop", phone_number_id="BRAND_PHONE_2",
        access_token="tok", user_id=owner.id,
    ))
    db_session.commit()

    assert catalog_sync.sync_catalog_item(str(p.id), "upsert") == "Upserted."
    (items,) = fake_client.instances[0].upserts
    assert items[0]["brand"] == "Juma's Shop"

    # No account either: the personal name is next in the chain.
    db_session.query(WhatsAppAccount).filter(
        WhatsAppAccount.user_id == owner.id
    ).delete()
    db_session.commit()
    fake_client.instances.clear()

    assert catalog_sync.sync_catalog_item(str(p.id), "upsert") == "Upserted."
    (items,) = fake_client.instances[-1].upserts
    assert items[0]["brand"] == "Juma"


def test_task_meta_down_returns_failure_without_raising(
    db_session, product, catalog_creds, fake_client, caplog
):
    # Fail every client the task constructs.
    original = fake_client.__init__

    def always_fail(self, catalog_id, access_token):
        original(self, catalog_id, access_token)
        self.fail_with = CatalogError("meta 500")

    fake_client.__init__ = always_fail

    result = catalog_sync.sync_catalog_item(str(product.id), "upsert")

    assert result.startswith("Failed:")
    assert "meta 500" in caplog.text


def test_task_meta_down_eager_end_to_end_keeps_local_write(
    db_session, merchant, catalog_creds, fake_client
):
    # Real eager apply_async (no `dispatched` stub): the task runs inline and
    # Meta is down, but the local write must still succeed.
    original = fake_client.__init__

    def always_fail(self, catalog_id, access_token):
        original(self, catalog_id, access_token)
        self.fail_with = CatalogError("meta down")

    fake_client.__init__ = always_fail

    created = crud_product.create_product(
        db_session,
        ProductCreate(name="Resilient", max_price=1000, min_price=900, quantity=1),
        merchant.id,
    )

    assert db_session.get(Product, created.id) is not None
    assert fake_client.instances  # the sync was attempted


def test_task_delete_calls_delete_items_for_missing_row(catalog_creds, fake_client):
    gone_id = str(uuid.uuid4())

    result = catalog_sync.sync_catalog_item(gone_id, "delete")

    assert result == "Deleted."
    assert fake_client.instances[0].deletes == [[gone_id]]


def test_task_upsert_for_deleted_product_skips(db_session, catalog_creds, fake_client, caplog):
    result = catalog_sync.sync_catalog_item(str(uuid.uuid4()), "upsert")

    assert result == "Product not found; skipped."
    assert fake_client.instances[0].upserts == []
    assert "deleted between dispatch and execution" in caplog.text


def test_task_creds_unset_never_builds_client(db_session, product, fake_client):
    # No catalog_creds: conftest scrubs CATALOG_* to None.
    result = catalog_sync.sync_catalog_item(str(product.id), "upsert")

    assert result == "Catalog sync disabled."
    assert fake_client.instances == []


def test_task_unknown_op_logs_and_returns(db_session, product, catalog_creds, fake_client, caplog):
    result = catalog_sync.sync_catalog_item(str(product.id), "explode")

    assert result.startswith("Unknown op")
    assert fake_client.instances == []
    assert "unknown catalog sync op" in caplog.text.lower()


def test_task_batch_item_errors_logged_loudly(db_session, product, catalog_creds, fake_client, caplog):
    original = fake_client.__init__

    def with_errors(self, catalog_id, access_token):
        original(self, catalog_id, access_token)
        self.status_payload = {
            "data": [
                {
                    "handle": "handle_1",
                    "status": "finished",
                    "errors": [{"message": "bad price"}],
                    "errors_total_count": 1,
                }
            ]
        }

    fake_client.__init__ = with_errors

    result = catalog_sync.sync_catalog_item(str(product.id), "upsert")

    assert result == "Upserted."
    assert "bad price" in caplog.text


@pytest.mark.parametrize("pending_status", ["in_progress", "started"])
def test_task_pending_batch_warns_after_bounded_polls(
    db_session, product, catalog_creds, fake_client, monkeypatch, caplog,
    pending_status,
):
    original = fake_client.__init__

    def always_pending(self, catalog_id, access_token):
        original(self, catalog_id, access_token)
        self.status_payload = {
            "data": [{"handle": "handle_1", "status": pending_status, "errors": []}]
        }

    fake_client.__init__ = always_pending
    monkeypatch.setattr(catalog_sync, "STATUS_POLL_ATTEMPTS", 2)
    sleeps = []
    monkeypatch.setattr(catalog_sync.time, "sleep", lambda s: sleeps.append(s))

    result = catalog_sync.sync_catalog_item(str(product.id), "upsert")

    assert result == "Upserted."
    assert len(fake_client.instances[0].status_calls) == 2
    assert sleeps == [catalog_sync.STATUS_POLL_DELAY_SECONDS]
    assert "still pending" in caplog.text
