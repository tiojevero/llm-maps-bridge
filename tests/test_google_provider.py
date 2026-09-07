"""Unit tests for GoogleMapsProvider (Google client mocked; no real calls)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from googlemaps.exceptions import ApiError

from api.providers import google_provider
from api.providers.base import (
    InvalidApiKeyError,
    MapsNetworkError,
    NoResultsFoundError,
    QuotaExceededError,
)
from api.providers.google_provider import GoogleMapsProvider

SAMPLE_RESPONSE = {
    "status": "OK",
    "results": [
        {
            "name": "Sushi Place",
            "formatted_address": "Jl. Test No. 1, Malang",
            "place_id": "abc123",
            "geometry": {"location": {"lat": -7.98, "lng": 112.62}},
        },
        {
            "name": "Sushi Bar",
            "formatted_address": "Jl. Test No. 2, Malang",
            "place_id": "def456",
            "geometry": {"location": {"lat": -7.97, "lng": 112.63}},
        },
    ],
}


def _mock_client(places_return=None, places_side_effect=None):  # type: ignore[no-untyped-def]
    client = MagicMock()
    if places_side_effect is not None:
        client.places.side_effect = places_side_effect
    else:
        client.places.return_value = places_return
    return client


def _mock_nearby_client(nearby_return=None):  # type: ignore[no-untyped-def]
    client = MagicMock()
    client.places_nearby.return_value = nearby_return
    return client


NEARBY_RESPONSE = {
    "status": "OK",
    "results": [
        {
            "name": "Apotek Sehat",
            "vicinity": "Jl. Test No. 1, Malang",
            "place_id": "xyz789",
            "geometry": {"location": {"lat": -7.98, "lng": 112.62}},
        },
    ],
}


@pytest.fixture(autouse=True)
def _clean_cache():
    google_provider.clear_cache()
    yield
    google_provider.clear_cache()


@pytest.fixture()
def provider() -> GoogleMapsProvider:
    """Fresh Google provider instance per test."""
    return GoogleMapsProvider()


def test_find_places_success(provider: GoogleMapsProvider) -> None:
    """A successful query returns normalized dicts with expected keys."""
    with patch.object(
        google_provider, "_get_client", return_value=_mock_client(SAMPLE_RESPONSE)
    ):
        results = provider.find_places("sushi", "Malang")
    assert len(results) == 2
    first = results[0]
    assert first["name"] == "Sushi Place"
    assert first["address"] == "Jl. Test No. 1, Malang"
    assert first["lat"] == pytest.approx(-7.98)
    assert first["lng"] == pytest.approx(112.62)
    assert first["place_id"] == "abc123"
    assert first["maps_url"].startswith("https://www.google.com/maps/search/")


def test_find_places_zero_results(provider: GoogleMapsProvider) -> None:
    """A zero-results response raises NoResultsFoundError."""
    with patch.object(
        google_provider,
        "_get_client",
        return_value=_mock_client({"status": "ZERO_RESULTS", "results": []}),
    ):
        with pytest.raises(NoResultsFoundError):
            provider.find_places("asdfghjkl nowhere")


def test_find_places_quota_exceeded(provider: GoogleMapsProvider) -> None:
    """OVER_QUERY_LIMIT from Google raises QuotaExceededError."""
    with patch.object(
        google_provider,
        "_get_client",
        return_value=_mock_client(places_side_effect=ApiError("OVER_QUERY_LIMIT")),
    ):
        with pytest.raises(QuotaExceededError):
            provider.find_places("sushi")


def test_find_places_request_denied(provider: GoogleMapsProvider) -> None:
    """REQUEST_DENIED from Google raises InvalidApiKeyError."""
    with patch.object(
        google_provider,
        "_get_client",
        return_value=_mock_client(places_side_effect=ApiError("REQUEST_DENIED")),
    ):
        with pytest.raises(InvalidApiKeyError):
            provider.find_places("sushi")


def test_find_places_network_error(provider: GoogleMapsProvider) -> None:
    """Timeouts from the Google client raise MapsNetworkError."""
    from googlemaps.exceptions import Timeout

    with patch.object(
        google_provider,
        "_get_client",
        return_value=_mock_client(places_side_effect=Timeout()),
    ):
        with pytest.raises(MapsNetworkError):
            provider.find_places("sushi")


def test_find_places_empty_query(provider: GoogleMapsProvider) -> None:
    """Empty queries are rejected before any API call."""
    with pytest.raises(ValueError):
        provider.find_places("   ")


def test_find_places_cache_avoids_repeat_calls(
    provider: GoogleMapsProvider,
) -> None:
    """Identical queries within the TTL hit the cache (one billable call)."""
    client = _mock_client(SAMPLE_RESPONSE)
    with patch.object(google_provider, "_get_client", return_value=client):
        provider.find_places("sushi", "Malang")
        provider.find_places("sushi", "Malang")
    assert client.places.call_count == 1


def test_maps_client_shim_delegates() -> None:
    """The backwards-compat shim delegates to GoogleMapsProvider."""
    from api import maps_client

    with patch.object(
        google_provider, "_get_client", return_value=_mock_client(SAMPLE_RESPONSE)
    ):
        google_provider.clear_cache()
        results = maps_client.find_places("sushi", "Malang")
    assert results[0]["place_id"] == "abc123"
    assert maps_client.NoResultsFoundError is NoResultsFoundError


def test_find_nearby_success(provider: GoogleMapsProvider) -> None:
    """Nearby Search maps to a type and normalizes vicinity addresses."""
    with patch.object(
        google_provider,
        "_get_client",
        return_value=_mock_nearby_client(NEARBY_RESPONSE),
    ) as mock_get_client:
        results = provider.find_nearby("pharmacy", -7.98, 112.62, 1500)
    client = mock_get_client.return_value
    _, kwargs = client.places_nearby.call_args
    assert kwargs["type"] == "pharmacy"
    assert kwargs["radius"] == 1500
    assert len(results) == 1
    assert results[0]["name"] == "Apotek Sehat"
    assert results[0]["address"] == "Jl. Test No. 1, Malang"
    assert results[0]["place_id"] == "xyz789"


def test_find_nearby_unknown_category_uses_keyword(
    provider: GoogleMapsProvider,
) -> None:
    """Unmapped categories fall back to keyword search."""
    with patch.object(
        google_provider,
        "_get_client",
        return_value=_mock_nearby_client(NEARBY_RESPONSE),
    ) as mock_get_client:
        provider.find_nearby("laundromat", -7.98, 112.62)
    _, kwargs = mock_get_client.return_value.places_nearby.call_args
    assert "type" not in kwargs
    assert kwargs["keyword"] == "laundromat"


def test_find_nearby_invalid_inputs(provider: GoogleMapsProvider) -> None:
    """Bad category/coords/radius are rejected before any API call."""
    with pytest.raises(ValueError):
        provider.find_nearby("   ", -7.98, 112.62)
    with pytest.raises(ValueError):
        provider.find_nearby("pharmacy", -91.0, 112.62)
    with pytest.raises(ValueError):
        provider.find_nearby("pharmacy", -7.98, 112.62, 0)
