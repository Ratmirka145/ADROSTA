from __future__ import annotations

import os

import pytest

from app.integrations import CdekClient


@pytest.mark.cdek_live
def test_cdek_test_environment_smoke() -> None:
    required = ("CDEK_CLIENT_ID", "CDEK_CLIENT_SECRET", "CDEK_FROM_CITY_CODE")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip("CDEK test credentials/config are not configured")
    if os.getenv("CDEK_ENV", "test").casefold() != "test":
        pytest.skip("live smoke is deliberately restricted to CDEK_ENV=test")

    client = CdekClient(
        client_id=os.environ["CDEK_CLIENT_ID"],
        client_secret=os.environ["CDEK_CLIENT_SECRET"],
        environment="test",
        timeout_seconds=float(os.getenv("CDEK_HTTP_TIMEOUT_SECONDS", "10")),
    )
    try:
        cities = client.cities(query="Москва", country_code="RU")
        assert cities
        city = next(
            (item for item in cities if item.get("city") == "Москва"),
            cities[0],
        )
        city_code = int(city["code"])
        offices = client.delivery_points(city_code=city_code)
        assert offices
        tariffs = client.tariff_list(
            {
                "type": 1,
                "currency": 1,
                "lang": "rus",
                "from_location": {"code": int(os.environ["CDEK_FROM_CITY_CODE"])},
                "to_location": {"code": city_code},
                "packages": [
                    {
                        "number": "1",
                        "weight": 10_900,
                        "length": 33,
                        "width": 20,
                        "height": 27,
                    }
                ],
            }
        )
        assert tariffs
    finally:
        client.close()
