"""Unit tests for OsmProvider (HTTP mocked; no real network calls)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from api.providers import osm_provider
from api.providers.base import (
    MapsNetworkError,
    NoResultsFoundError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
    QuotaExceededError,
)
from api.providers.osm_provider import OsmProvider

NOMINATIM_ITEM = {
    "osm_id": 12345,
    "osm_type": "node",
    "lat": "-7.98",
    "lon": "112.62",
    "display_name": "Sushi Place, Jl. Test No. 1, Malang, Indonesia",
}

OSRM_OK = {"code": "Ok", "routes": [{"distance": 2100.0, "duration": 540.0}]}

OVERPASS_OK = {
    "elements": [
        {
            "type": "node",
            "id": 111,
            "lat": -7.98,
            "lon": 112.62,
            "tags": {
                "name": "Apotek Sehat",
                "addr:housenumber": "1",
                "addr:street": "Jl. Test",
                "addr:city": "Malang",
            },
        },
        {
            "type": "way",
            "id": 222,
            "center": {"lat": -7.97, "lon": 112.63},
            "tags": {"name": "Apotek Kimia"},
        },
    ]
}


def _mock_response(payload, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch) -> None:
    osm_provider.clear_cache()
    # Disable the 1 req/s politeness gate so tests run fast.
    monkeypatch.setattr(osm_provider, "NOMINATIM_MIN_INTERVAL_SECONDS", 0)
    yield
    osm_provider.clear_cache()


@pytest.fixture()
def provider() -> OsmProvider:
    """Fresh OSM provider instance per test."""
    return OsmProvider()


def test_find_places_success(provider: OsmProvider) -> None:
    """A successful query returns the same normalized shape as Google."""
    with patch.object(
        osm_provider, "_http_get", return_value=_mock_response([NOMINATIM_ITEM])
    ) as http_get:
        results = provider.find_places("sushi", "Malang")
    assert http_get.call_count == 1
    # Comma-joined region bias: Nominatim matches this far better than
    # Google-style "sushi in Malang".
    assert http_get.call_args.kwargs["params"]["q"] == "sushi, Malang"
    assert len(results) == 1
    first = results[0]
    assert first["name"] == "Sushi Place"
    assert first["address"] == NOMINATIM_ITEM["display_name"]
    assert first["lat"] == pytest.approx(-7.98)
    assert first["lng"] == pytest.approx(112.62)
    assert first["place_id"] == "node/12345"
    assert first["maps_url"].startswith("https://www.openstreetmap.org/")


def test_exception_hierarchy_compat() -> None:
    """Old exception names stay catchable via the new shared names."""
    assert issubclass(QuotaExceededError, ProviderRateLimitedError)
    assert issubclass(MapsNetworkError, ProviderUnavailableError)


def test_find_places_zero_results(provider: OsmProvider) -> None:
    """An empty Nominatim list raises NoResultsFoundError."""
    with patch.object(osm_provider, "_http_get", return_value=_mock_response([])):
        with pytest.raises(NoResultsFoundError):
            provider.find_places("asdfghjkl nowhere")


def test_find_places_network_error(provider: OsmProvider) -> None:
    """Timeouts raise MapsNetworkError (same type as the Google provider)."""
    with patch.object(
        osm_provider,
        "_http_get",
        side_effect=httpx.TimeoutException("timed out"),
    ):
        with pytest.raises(MapsNetworkError):
            provider.find_places("sushi")


def test_find_places_rate_limited(provider: OsmProvider) -> None:
    """HTTP 429 from Nominatim maps to QuotaExceededError."""
    with patch.object(osm_provider, "_http_get", return_value=_mock_response({}, 429)):
        with pytest.raises(QuotaExceededError):
            provider.find_places("sushi")


def test_find_places_empty_query(provider: OsmProvider) -> None:
    """Empty queries are rejected before any HTTP call."""
    with pytest.raises(ValueError):
        provider.find_places("   ")


def test_find_places_cache_avoids_repeat_calls(provider: OsmProvider) -> None:
    """Identical queries within the TTL hit the cache (one upstream call)."""
    with patch.object(
        osm_provider, "_http_get", return_value=_mock_response([NOMINATIM_ITEM])
    ) as http_get:
        provider.find_places("sushi", "Malang")
        provider.find_places("sushi", "Malang")
    assert http_get.call_count == 1


def test_get_directions_success(provider: OsmProvider) -> None:
    """Origin is geocoded, OSRM returns distance/duration in Google style."""
    origin_item = dict(
        NOMINATIM_ITEM,
        osm_id=999,
        lat="-7.97",
        lon="112.60",
        display_name="Malang station, Malang, Indonesia",
    )
    with patch.object(
        osm_provider,
        "_http_get",
        side_effect=[
            _mock_response([NOMINATIM_ITEM]),  # find_places search
            _mock_response([origin_item]),  # origin geocode
            _mock_response(OSRM_OK),  # OSRM route
        ],
    ):
        places = provider.find_places("sushi", "Malang")
        route = provider.get_directions("Malang station", places[0]["place_id"])
    assert route["distance"] == "2.1 km"
    assert route["duration"] == "9 mins"
    assert route["origin"] == "Malang station"
    assert route["destination_place_id"] == "node/12345"


def test_get_directions_unknown_place_id(provider: OsmProvider) -> None:
    """A place_id never returned by find_places raises NoResultsFoundError."""
    with pytest.raises(NoResultsFoundError):
        provider.get_directions("Malang station", "no-such-place")


def test_get_directions_no_route(provider: OsmProvider) -> None:
    """An empty OSRM routes list raises NoResultsFoundError."""
    with patch.object(
        osm_provider,
        "_http_get",
        side_effect=[
            _mock_response([NOMINATIM_ITEM]),
            _mock_response([NOMINATIM_ITEM]),
            _mock_response({"code": "Ok", "routes": []}),
        ],
    ):
        places = provider.find_places("sushi")
        with pytest.raises(NoResultsFoundError):
            provider.get_directions("Malang station", places[0]["place_id"])


def test_get_directions_empty_origin(provider: OsmProvider) -> None:
    """Empty origins are rejected before any HTTP call."""
    with pytest.raises(ValueError):
        provider.get_directions("   ", "node/12345")


def test_find_nearby_success(provider: OsmProvider) -> None:
    """Overpass results map to the shared shape with type/id place_ids."""
    with patch.object(
        osm_provider, "_http_post", return_value=_mock_response(OVERPASS_OK)
    ) as http_post:
        results = provider.find_nearby("pharmacy", -7.98, 112.62, 1500)
    assert http_post.call_count == 1
    posted = http_post.call_args.kwargs["data"]["data"]
    assert '"amenity"="pharmacy"' in posted
    assert "around:1500" in posted
    assert len(results) == 2
    assert results[0]["name"] == "Apotek Sehat"
    assert results[0]["address"] == "1 Jl. Test, Malang"
    assert results[0]["place_id"] == "node/111"
    assert results[1]["place_id"] == "way/222"
    assert results[1]["lat"] == pytest.approx(-7.97)
    for result in results:
        assert result["maps_url"].startswith("https://www.openstreetmap.org/")


def test_find_nearby_category_mapping(provider: OsmProvider) -> None:
    """Known categories map to OSM tags; unknown ones get a best guess."""
    with patch.object(
        osm_provider, "_http_post", return_value=_mock_response(OVERPASS_OK)
    ) as http_post:
        provider.find_nearby("coffee", -7.98, 112.62)
    assert '"amenity"="cafe"' in http_post.call_args.kwargs["data"]["data"]

    osm_provider.clear_cache()
    with patch.object(
        osm_provider, "_http_post", return_value=_mock_response(OVERPASS_OK)
    ) as http_post:
        provider.find_nearby("laundromat", -7.98, 112.62)
    assert '"amenity"="laundromat"' in http_post.call_args.kwargs["data"]["data"]


def test_find_nearby_no_results(provider: OsmProvider) -> None:
    """Empty Overpass elements raise NoResultsFoundError."""
    with patch.object(
        osm_provider, "_http_post", return_value=_mock_response({"elements": []})
    ):
        with pytest.raises(NoResultsFoundError):
            provider.find_nearby("pharmacy", -7.98, 112.62)


def test_find_nearby_timeout(provider: OsmProvider) -> None:
    """Overpass timeouts raise MapsNetworkError (same type as Google)."""
    with patch.object(
        osm_provider,
        "_http_post",
        side_effect=httpx.TimeoutException("timed out"),
    ):
        with pytest.raises(MapsNetworkError):
            provider.find_nearby("pharmacy", -7.98, 112.62)


def test_find_nearby_invalid_inputs(provider: OsmProvider) -> None:
    """Bad category/coords/radius are rejected before any HTTP call."""
    with pytest.raises(ValueError):
        provider.find_nearby("   ", -7.98, 112.62)
    with pytest.raises(ValueError):
        provider.find_nearby("pharmacy", -91.0, 112.62)
    with pytest.raises(ValueError):
        provider.find_nearby("pharmacy", -7.98, 112.62, 0)
