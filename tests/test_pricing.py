from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.domain import AmbiguousPriceTierError, PriceTier, Product, calculate_order
from app.models import IdempotencyRecordModel, OrderItemModel, OrderModel, OutboxModel
from app.repositories import Product as StoredProduct
from app.repositories import ProductPriceTier
from app.schemas import OrderItem


def _seed_adrosta(application: FastAPI) -> None:
    products = application.state.context.products
    tiers = (
        ProductPriceTier(1, 4, 32_000),
        ProductPriceTier(5, 9, 29_000),
        ProductPriceTier(10, None, 26_000),
    )
    for sku, name in (
        ("opt-san-green", "SAN Green"),
        ("opt-san-blue", "SAN Blue"),
    ):
        products.create(
            StoredProduct(
                sku=sku,
                name=name,
                units_per_box=10,
                box_weight_grams=10_900,
                box_volume_mm3=330 * 200 * 265,
                box_length_mm=330,
                box_width_mm=200,
                box_height_mm=265,
            )
        )
        products.replace_price_tiers(sku, tiers)


@pytest.mark.parametrize(
    ("boxes", "price", "amount"),
    [
        (1, 32_000, 320_000),
        (4, 32_000, 1_280_000),
        (5, 29_000, 1_450_000),
        (9, 29_000, 2_610_000),
        (10, 26_000, 2_600_000),
        (20, 26_000, 5_200_000),
    ],
)
def test_cart_calculation_uses_per_sku_tier_boundaries(
    app_factory: Callable[..., FastAPI],
    boxes: int,
    price: int,
    amount: int,
) -> None:
    application = app_factory(seed_products=False)
    _seed_adrosta(application)

    with TestClient(application) as client:
        response = client.post(
            "/api/cart/calculate",
            json={"items": [{"sku": "opt-san-green", "boxes": boxes}]},
        )

    assert response.status_code == 200
    body = response.json()
    item = body["items"][0]
    assert set(item) == {
        "sku",
        "name",
        "boxes",
        "unitsPerBox",
        "units",
        "pricePerUnitKopecks",
        "pricePerBoxKopecks",
        "lineAmountKopecks",
        "weightPerBoxGrams",
        "totalWeightGrams",
        "boxVolumeMm3",
        "lengthMm",
        "widthMm",
        "heightMm",
        "cargoPlaces",
        "totalVolumeMm3",
    }
    assert set(body["totals"]) == {
        "totalBoxes",
        "totalUnits",
        "productsAmountKopecks",
        "totalWeightGrams",
        "cargoPlaces",
        "totalVolumeMm3",
    }
    assert not {
        "weightKg",
        "volumeM3",
        "boxWeightGrams",
        "weightGrams",
        "volumeMm3",
    } & set(item)
    assert not {
        "boxes",
        "weightKg",
        "volumeM3",
        "weightGrams",
        "volumeMm3",
    } & set(body["totals"])
    assert item["sku"] == "opt-san-green"
    assert item["name"] == "SAN Green"
    assert item["boxes"] == boxes
    assert item["unitsPerBox"] == 10
    assert item["units"] == boxes * 10
    assert item["pricePerUnitKopecks"] == price
    assert item["pricePerBoxKopecks"] == price * 10
    assert item["lineAmountKopecks"] == amount
    assert item["weightPerBoxGrams"] == 10_900
    assert item["totalWeightGrams"] == boxes * 10_900
    assert (item["lengthMm"], item["widthMm"], item["heightMm"]) == (
        330,
        200,
        265,
    )
    assert item["cargoPlaces"] == boxes
    assert body["totals"]["productsAmountKopecks"] == amount
    assert body["totals"]["totalBoxes"] == boxes
    assert body["totals"]["totalUnits"] == boxes * 10
    assert body["totals"]["cargoPlaces"] == boxes
    assert body["totals"]["totalVolumeMm3"] == boxes * 17_490_000

    with application.state.context.database.session() as session:
        for model in (OrderModel, OutboxModel, IdempotencyRecordModel):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_cart_calculates_tier_separately_for_each_sku(
    app_factory: Callable[..., FastAPI],
) -> None:
    application = app_factory(seed_products=False)
    _seed_adrosta(application)

    with TestClient(application) as client:
        response = client.post(
            "/api/cart/calculate",
            json={
                "items": [
                    {"sku": "opt-san-green", "boxes": 6},
                    {"sku": "opt-san-blue", "boxes": 4},
                ]
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["pricePerUnitKopecks"] == 29_000
    assert body["items"][0]["lineAmountKopecks"] == 1_740_000
    assert body["items"][1]["pricePerUnitKopecks"] == 32_000
    assert body["items"][1]["lineAmountKopecks"] == 1_280_000
    assert body["totals"]["totalBoxes"] == 10
    assert body["totals"]["totalUnits"] == 100
    assert body["totals"]["productsAmountKopecks"] == 3_020_000
    assert body["totals"]["totalWeightGrams"] == 109_000
    assert body["totals"]["cargoPlaces"] == 10
    assert body["totals"]["totalVolumeMm3"] == 174_900_000


def test_openapi_documents_only_canonical_calculation_fields(
    app_factory: Callable[..., FastAPI],
) -> None:
    application = app_factory()
    schemas = application.openapi()["components"]["schemas"]

    assert set(schemas["CalculatedItemResponse"]["properties"]) == {
        "sku",
        "name",
        "boxes",
        "unitsPerBox",
        "units",
        "pricePerUnitKopecks",
        "pricePerBoxKopecks",
        "lineAmountKopecks",
        "weightPerBoxGrams",
        "totalWeightGrams",
        "boxVolumeMm3",
        "lengthMm",
        "widthMm",
        "heightMm",
        "cargoPlaces",
        "totalVolumeMm3",
    }
    assert set(schemas["OrderTotalsResponse"]["properties"]) == {
        "totalBoxes",
        "totalUnits",
        "productsAmountKopecks",
        "totalWeightGrams",
        "cargoPlaces",
        "totalVolumeMm3",
    }


def test_cart_rejects_unknown_sku_without_creating_order(
    app_factory: Callable[..., FastAPI],
) -> None:
    application = app_factory(seed_products=False)
    _seed_adrosta(application)
    with TestClient(application) as client:
        response = client.post(
            "/api/cart/calculate",
            json={"items": [{"sku": "fake-product", "boxes": 5}]},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNKNOWN_SKU"


@pytest.mark.parametrize("boxes", [0, -1, 1.5, "five"])
def test_cart_rejects_invalid_boxes(
    client: TestClient,
    boxes: object,
) -> None:
    response = client.post(
        "/api/cart/calculate",
        json={"items": [{"sku": "ADR-001", "boxes": boxes}]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_cart_rejects_client_price_override(client: TestClient) -> None:
    response = client.post(
        "/api/cart/calculate",
        json={
            "items": [
                {
                    "sku": "ADR-001",
                    "boxes": 5,
                    "pricePerUnitKopecks": 1,
                }
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"][0]["code"] == "UNKNOWN_FIELD"


def test_missing_price_tier_is_a_controlled_catalog_error(
    app_factory: Callable[..., FastAPI],
) -> None:
    application = app_factory()
    application.state.context.products.replace_price_tiers("ADR-001", ())
    with TestClient(application) as client:
        response = client.post(
            "/api/cart/calculate",
            json={"items": [{"sku": "ADR-001", "boxes": 1}]},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PRICE_TIER_NOT_FOUND"


def test_domain_rejects_overlapping_price_tiers() -> None:
    product = Product(
        sku="overlap",
        name="Overlap",
        units_per_box=10,
        weight_grams=1,
        length_mm=1,
        width_mm=1,
        height_mm=1,
        price_tiers=(PriceTier(1, 10, 100), PriceTier(5, None, 90)),
    )
    with pytest.raises(AmbiguousPriceTierError):
        calculate_order([OrderItem(sku="overlap", boxes=5)], {"overlap": product})


def test_order_persists_calculation_and_price_snapshot(
    app_factory: Callable[..., FastAPI],
    order_payload_factory: Callable[..., dict[str, object]],
) -> None:
    application = app_factory(seed_products=False)
    _seed_adrosta(application)
    payload = order_payload_factory()
    payload["items"] = [{"sku": "opt-san-green", "boxes": 5}]

    with TestClient(application) as client:
        response = client.post(
            "/api/orders",
            json=payload,
            headers={"Idempotency-Key": "pricing-snapshot-order-0001"},
        )
    assert response.status_code == 201
    order_id = response.json()["orderId"]

    application.state.context.products.replace_price_tiers(
        "opt-san-green",
        (ProductPriceTier(1, None, 31_000),),
    )
    with application.state.context.database.session() as session:
        item = session.scalar(
            select(OrderItemModel).where(OrderItemModel.order_id == order_id)
        )
        order = session.get(OrderModel, order_id)
        assert item is not None
        assert order is not None
        item_values = {
            name: getattr(item, name)
            for name in (
                "boxes", "units_per_box", "units", "price_per_unit_kopecks",
                "price_per_box_kopecks", "line_amount_kopecks",
                "total_weight_grams", "cargo_places",
            )
        }
        order_values = {
            name: getattr(order, name)
            for name in (
                "total_boxes", "total_units", "products_amount_kopecks",
                "total_weight_grams", "cargo_places",
            )
        }

    assert item_values == {
        "boxes": 5,
        "units_per_box": 10,
        "units": 50,
        "price_per_unit_kopecks": 29_000,
        "price_per_box_kopecks": 290_000,
        "line_amount_kopecks": 1_450_000,
        "total_weight_grams": 54_500,
        "cargo_places": 5,
    }
    assert order_values == {
        "total_boxes": 5,
        "total_units": 50,
        "products_amount_kopecks": 1_450_000,
        "total_weight_grams": 54_500,
        "cargo_places": 5,
    }
    stored = application.state.context.orders.get_order_response(order_id)
    assert stored is not None
    assert stored.items[0].price_per_unit_kopecks == 29_000
