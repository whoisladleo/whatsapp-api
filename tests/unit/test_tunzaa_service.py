import json
from datetime import datetime, timedelta

import httpx
import respx

from app.db.models import Customer, Order
from app.services.tunzaa import TunzaaService

TOKEN_URL = "https://pay.tunzaa.co.tz/api/v1/accounts/request/token"
PAYMENT_URL = "https://pay.tunzaa.co.tz/api/v1/payments/initiate-payment"
INSTALLMENT_URL = "https://pay.tunzaa.co.tz/api/v1/installments/create"


def _service():
    return TunzaaService(api_key="key", secret_key="secret")


@respx.mock
async def test_get_token_fetches_and_caches(monkeypatch):
    service = _service()
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )

    token1 = await service.get_token()
    token2 = await service.get_token()

    assert token1 == "tok-1"
    assert token2 == "tok-1"
    assert route.call_count == 1  # second call served from cache


@respx.mock
async def test_get_token_refetches_after_expiry():
    service = _service()
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1"}),
            httpx.Response(200, json={"access_token": "tok-2"}),
        ]
    )

    token1 = await service.get_token()
    service._token_expiry = datetime.utcnow() - timedelta(seconds=1)
    token2 = await service.get_token()

    assert token1 == "tok-1"
    assert token2 == "tok-2"
    assert route.call_count == 2


@respx.mock
async def test_initiate_payment_payload_and_auth_header():
    service = _service()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )
    payment_route = respx.post(PAYMENT_URL).mock(
        return_value=httpx.Response(200, json={"status": "PENDING"})
    )

    result = await service.initiate_payment("255711111111", 45000, "order-1")

    assert result == {"status": "PENDING"}
    sent = json.loads(payment_route.calls.last.request.content)
    assert sent == {
        "customer_msisdn": "255711111111",
        "amount": "45000",
        "reference": "order-1",
    }
    assert payment_route.calls.last.request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_create_installment_plan_payload(db_session):
    customer = Customer(phone_number="255711111111", delivery_address="Dar es Salaam")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    order = Order(customer_id=customer.id, agreed_price=90000)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    order.customer  # ensure relationship is loaded before the async call

    service = _service()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )
    installment_route = respx.post(INSTALLMENT_URL).mock(
        return_value=httpx.Response(200, json={"id": "plan-1"})
    )

    result = await service.create_installment_plan(order, "Shoes")

    assert result == {"id": "plan-1"}
    sent = json.loads(installment_route.calls.last.request.content)
    assert sent["customer"]["phone"] == "255711111111"
    assert sent["customer"]["address"] == "Dar es Salaam"
    assert sent["total_amount"] == 90000.0
    assert sent["name"] == "Shoes - 3 Month Plan"
    assert sent["payment_frequency"] == "monthly"


@respx.mock
async def test_initiate_payment_raises_on_http_error():
    service = _service()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )
    respx.post(PAYMENT_URL).mock(return_value=httpx.Response(500))

    import pytest

    with pytest.raises(httpx.HTTPStatusError):
        await service.initiate_payment("255711111111", 1000, "order-2")


@respx.mock
async def test_get_token_raises_on_http_error():
    service = _service()
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))

    import pytest

    with pytest.raises(httpx.HTTPStatusError):
        await service.get_token()


@respx.mock
async def test_create_installment_plan_raises_on_http_error(db_session):
    customer = Customer(phone_number="255711119999")
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    order = Order(customer_id=customer.id, agreed_price=10000)
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    order.customer

    service = _service()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1"})
    )
    respx.post(INSTALLMENT_URL).mock(return_value=httpx.Response(500))

    import pytest

    with pytest.raises(httpx.HTTPStatusError):
        await service.create_installment_plan(order, "Shoes")
