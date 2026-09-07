"""Backwards-compatible shim for the former Google client module.

The implementation now lives in :mod:`api.providers.google_provider`
behind the :class:`~api.providers.base.PlacesProvider` interface.
New code should use ``config.get_places_provider()`` instead of
importing this module directly.

This shim delegates to a shared :class:`GoogleMapsProvider` instance
so existing imports keep working unchanged.
"""

from __future__ import annotations

from api import config
from api.providers.base import (
    InvalidApiKeyError,
    MapsNetworkError,
    NoResultsFoundError,
    PlacesError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
    QuotaExceededError,
)
from api.providers.google_provider import GoogleMapsProvider, clear_cache

__all__ = [
    "InvalidApiKeyError",
    "MapsNetworkError",
    "NoResultsFoundError",
    "PlacesError",
    "ProviderRateLimitedError",
    "ProviderUnavailableError",
    "QuotaExceededError",
    "clear_cache",
    "find_places",
    "find_nearby",
    "get_directions",
]

_default_provider = GoogleMapsProvider()


def find_places(
    query: str, location_bias: str | None = None, limit: int = config.MAX_RESULTS
) -> list[dict]:
    """Find places via the shared Google provider (see its docstring)."""
    return _default_provider.find_places(query, location_bias, limit)


def find_nearby(
    category: str,
    lat: float,
    lng: float,
    radius_meters: int = 1500,
    limit: int = config.MAX_RESULTS,
) -> list[dict]:
    """Find nearby places via the shared Google provider (see its docstring)."""
    return _default_provider.find_nearby(category, lat, lng, radius_meters, limit)


def get_directions(origin: str, destination_place_id: str) -> dict:
    """Get a route summary via the shared Google provider (see its docstring)."""
    return _default_provider.get_directions(origin, destination_place_id)
