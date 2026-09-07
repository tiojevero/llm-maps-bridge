"""Integration tests for the FastAPI app (provider mocked; no real calls).

The endpoints resolve their provider per request via
``config.get_places_provider()``, so these tests patch that factory to
return a mock — mirroring the default OSM selection in CI without any
network access.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api import config, rate_limiter
from api.main import app
from api.providers.base import PlacesProvider, QuotaExceededError

SAMPLE_PLACES = [
    {
        "name": "Sushi Place",
        "address": "Jl. Test No. 1, Malang",
        "lat": -7.98,
        "lng": 112.62,
        "place_id": "abc123",
        "maps_url": "https://www.google.com/maps/search/?api=1&query=Sushi",
    }
]

SAMPLE_ROUTE = {
    "distance": "2.1 km",
    "duration": "9 mins",
    "origin": "Malang station",
    "destination_place_id": "abc123",
}


@pytest.fixture()
def client():
    """TestClient with rate-limit state reset before/after each test."""
    rate_limiter.reset_hits()
    with TestClient(app) as c:
        yield c
    rate_limiter.reset_hits()


@pytest.fixture()
def mock_provider(monkeypatch) -> MagicMock:
    """Patch the provider factory to return a mock (OSM-style default)."""
    provider = MagicMock(spec=PlacesProvider)
    provider.find_places.return_value = SAMPLE_PLACES
    provider.find_nearby.return_value = SAMPLE_PLACES
    provider.get_directions.return_value = dict(SAMPLE_ROUTE)
    monkeypatch.setattr(config, "get_places_provider", lambda: provider)
    return provider


def test_health(client: TestClient) -> None:
    """GET /health returns ok."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_search_success(client: TestClient, mock_provider: MagicMock) -> None:
    """Successful search returns embed HTML, fallback link, and results."""
    resp = client.post("/places/search", json={"query": "sushi", "near": "Malang"})
    assert resp.status_code == 200
    mock_provider.find_places.assert_called_once_with("sushi", "Malang")
    data = resp.json()
    assert "<iframe" in data["embed_html"]
    assert data["fallback_link"].startswith("https://www.google.com/maps/search/")
    assert data["results"][0]["place_id"] == "abc123"


def test_search_empty_query_returns_400(
    client: TestClient, mock_provider: MagicMock
) -> None:
    """Empty query is rejected with 400 and a clear message."""
    resp = client.post("/places/search", json={"query": "   "})
    assert resp.status_code == 400
    assert "query" in resp.json()["detail"].lower()
    mock_provider.find_places.assert_not_called()


def test_search_rate_limited_returns_429(
    client: TestClient, mock_provider: MagicMock, monkeypatch
) -> None:
    """Exceeding the backend rate limit returns 429."""
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 60)
    first = client.post("/places/search", json={"query": "sushi"})
    assert first.status_code == 200
    second = client.post("/places/search", json={"query": "sushi"})
    assert second.status_code == 429
    assert "rate limit" in second.json()["detail"].lower()


def test_search_provider_failure_returns_502(
    client: TestClient, mock_provider: MagicMock
) -> None:
    """Provider-side failures surface as 502, not 500."""
    mock_provider.find_places.side_effect = QuotaExceededError("quota exceeded")
    resp = client.post("/places/search", json={"query": "sushi"})
    assert resp.status_code == 502


def test_directions_success(client: TestClient, mock_provider: MagicMock) -> None:
    """Successful directions call returns embed HTML + summary."""
    resp = client.post(
        "/places/directions",
        json={"origin": "Malang station", "destination_place_id": "abc123"},
    )
    assert resp.status_code == 200
    mock_provider.get_directions.assert_called_once_with("Malang station", "abc123")
    data = resp.json()
    assert "<iframe" in data["directions_embed_html"]
    assert data["distance"] == "2.1 km"
    assert data["duration"] == "9 mins"


def test_directions_empty_origin_returns_400(
    client: TestClient, mock_provider: MagicMock
) -> None:
    """Empty origin is rejected with 400."""
    resp = client.post(
        "/places/directions",
        json={"origin": " ", "destination_place_id": "abc123"},
    )
    assert resp.status_code == 400
    mock_provider.get_directions.assert_not_called()


def test_provider_factory_defaults_to_osm(monkeypatch) -> None:
    """Unset MAPS_PROVIDER resolves to the OSM provider (no key needed)."""
    from api.providers.osm_provider import OsmProvider

    monkeypatch.delenv("MAPS_PROVIDER", raising=False)
    assert isinstance(config.get_places_provider(), OsmProvider)


def test_provider_factory_selects_google(monkeypatch) -> None:
    """MAPS_PROVIDER=google resolves once a key is provided."""
    from api.providers.google_provider import GoogleMapsProvider

    monkeypatch.setenv("MAPS_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "dummy-key")
    assert isinstance(config.get_places_provider(), GoogleMapsProvider)


def test_provider_factory_google_without_key_fails_fast(monkeypatch) -> None:
    """Google selected without a key errors clearly instead of falling back."""
    monkeypatch.setenv("MAPS_PROVIDER", "google")
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.setattr(config, "GOOGLE_MAPS_API_KEY", None)
    with pytest.raises(RuntimeError, match="MAPS_PROVIDER"):
        config.get_places_provider()


def test_google_mode_without_key_returns_502_not_500(
    client: TestClient, monkeypatch
) -> None:
    """Misconfigured google mode fails fast per request with a clear 502."""
    monkeypatch.setenv("MAPS_PROVIDER", "google")
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.setattr(config, "GOOGLE_MAPS_API_KEY", None)
    resp = client.post("/places/search", json={"query": "sushi"})
    assert resp.status_code == 502
    assert "MAPS_PROVIDER" in resp.json()["detail"]


def test_provider_factory_rejects_unknown(monkeypatch) -> None:
    """Unknown MAPS_PROVIDER values fail fast with a clear error."""
    monkeypatch.setenv("MAPS_PROVIDER", "bogus")
    with pytest.raises(ValueError, match="MAPS_PROVIDER"):
        config.get_places_provider()


def test_nearby_success(client: TestClient, mock_provider: MagicMock) -> None:
    """Successful nearby search returns the same shape as /places/search."""
    resp = client.post(
        "/places/nearby",
        json={"category": "pharmacy", "lat": -7.98, "lng": 112.62},
    )
    assert resp.status_code == 200
    mock_provider.find_nearby.assert_called_once_with("pharmacy", -7.98, 112.62, 1500)
    data = resp.json()
    assert "<iframe" in data["embed_html"]
    assert data["results"][0]["place_id"] == "abc123"


def test_nearby_invalid_coords_returns_400(
    client: TestClient, mock_provider: MagicMock
) -> None:
    """Out-of-range coordinates are rejected with 400."""
    resp = client.post(
        "/places/nearby",
        json={"category": "pharmacy", "lat": -91.0, "lng": 112.62},
    )
    assert resp.status_code == 400
    mock_provider.find_nearby.assert_not_called()


def test_nearby_no_results_returns_empty(
    client: TestClient, mock_provider: MagicMock
) -> None:
    """No nearby matches return 200 with a friendly message, like search."""
    from api.providers.base import NoResultsFoundError

    mock_provider.find_nearby.side_effect = NoResultsFoundError("nothing near")
    resp = client.post(
        "/places/nearby",
        json={"category": "pharmacy", "lat": -7.98, "lng": 112.62},
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []
