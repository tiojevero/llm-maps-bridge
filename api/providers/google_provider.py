"""Google Maps implementation of the places provider interface.

All Google Maps logic lives inside the backend — the Open
WebUI Tool never imports this module and never sees the API key.

Billing note: every uncached ``find_places`` call is billable. A short
TTL cache (5 minutes, keyed on normalized query) avoids repeat charges
for identical queries.
"""

from __future__ import annotations

import logging
import urllib.parse

import googlemaps
from googlemaps.exceptions import ApiError, Timeout, TransportError

from api import config
from api.providers._common import (
    CacheEntry as _CommonCacheEntry,
    cache_lookup,
    cache_store,
    normalize_query as _normalize_query_common,
    raise_for_google_api_error,
    raise_for_google_transport_error,
    require_non_empty_string,
    require_positive_int,
    validate_coords as _validate_coords_common,
)
from api.providers.base import (
    NoResultsFoundError,
    PlacesProvider,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 5 * 60


# Simple TTL dict keyed on normalized query string. Single-purpose for
# this test scope; a DB-backed cache would be next (see DECISIONS.md).
# The entry type lives in _common; this alias keeps existing imports working.
_CacheEntry = _CommonCacheEntry
_places_cache: dict[str, _CacheEntry] = {}


def _normalize_query(query: str, location_bias: str | None) -> str:
    """Normalize a query+bias pair into a stable cache key."""
    return _normalize_query_common(query, location_bias)


def _get_client() -> googlemaps.Client:
    """Build a Google Maps client from validated config.

    Returns:
        An authenticated ``googlemaps.Client``.

    Raises:
        RuntimeError: If the API key is missing (via config validation).
    """
    api_key = config.require_google_maps_api_key()
    return googlemaps.Client(key=api_key, timeout=config.MAPS_REQUEST_TIMEOUT_SECONDS)


# Maps natural-language categories to Google Places Nearby Search types.
# Deliberately small; unmapped categories fall back to keyword search.
_GOOGLE_PLACE_TYPES = {
    "restaurant": "restaurant",
    "cafe": "cafe",
    "coffee": "cafe",
    "pharmacy": "pharmacy",
    "hospital": "hospital",
    "atm": "atm",
    "gas station": "gas_station",
    "fuel": "gas_station",
    "hotel": "lodging",
    "bank": "bank",
    "bar": "bar",
    "supermarket": "supermarket",
}

# Google Places Nearby Search caps radius at 50 km.
MAX_NEARBY_RADIUS_METERS = 50000


def _build_maps_url(name: str, address: str, place_id: str) -> str:
    """Build a universal Maps URL for a place (no API key needed)."""
    query = urllib.parse.quote_plus(f"{name} {address}".strip() or name)
    # query_place_id anchors the result to the exact place.
    return (
        "https://www.google.com/maps/search/?api=1"
        f"&query={query}&query_place_id={urllib.parse.quote_plus(place_id)}"
    )


def _normalize_items(items: list[dict], limit: int) -> list[dict]:
    """Normalize raw Places API items to the shared result shape.

    Nearby Search returns ``vicinity`` where Text Search returns
    ``formatted_address``; both are accepted here.
    """
    normalized: list[dict] = []
    for item in items[:limit]:
        geometry = (item.get("geometry") or {}).get("location") or {}
        name = item.get("name", "Unknown place")
        address = item.get("formatted_address") or item.get("vicinity", "")
        place_id = item.get("place_id", "")
        normalized.append(
            {
                "name": name,
                "address": address,
                "lat": geometry.get("lat"),
                "lng": geometry.get("lng"),
                "place_id": place_id,
                "maps_url": _build_maps_url(name, address, place_id),
            }
        )
    return normalized


def _validate_coords(lat: float, lng: float) -> None:
    """Raise ValueError unless lat/lng are in valid ranges."""
    _validate_coords_common(lat, lng)


def clear_cache() -> None:
    """Clear the in-memory places cache (useful in tests)."""
    _places_cache.clear()


class GoogleMapsProvider(PlacesProvider):
    """Places provider backed by the Google Maps Platform (billable)."""

    def find_places(
        self,
        query: str,
        location_bias: str | None = None,
        limit: int = config.MAX_RESULTS,
    ) -> list[dict]:
        """Find places via Places Text Search, normalized and cached.

        See :meth:`PlacesProvider.find_places` for the contract.
        """
        query = require_non_empty_string(query, "query")
        limit = require_positive_int(limit, "limit")

        cache_key = _normalize_query(query, location_bias)
        cached = cache_lookup(_places_cache, cache_key)
        if cached is not None:
            logger.info("places cache hit for query=%r", query)
            return cached

        search_text = query
        if location_bias and location_bias.strip():
            search_text = f"{search_text} in {location_bias.strip()}"

        client = _get_client()
        logger.info("places search: query=%r", search_text)
        try:
            response = client.places(query=search_text)
        except ApiError as exc:
            raise_for_google_api_error(
                exc,
                operation="Places",
                no_results_message=f"No places found for {search_text!r}.",
            )
        except (Timeout, TransportError) as exc:
            raise_for_google_transport_error(exc, operation="Places")

        results = (response or {}).get("results", [])
        status = (response or {}).get("status", "OK")
        if status == "ZERO_RESULTS" or not results:
            raise NoResultsFoundError(f"No places found for {search_text!r}.")

        normalized: list[dict] = _normalize_items(results, limit)
        return cache_store(_places_cache, cache_key, normalized, CACHE_TTL_SECONDS)

    def find_nearby(
        self,
        category: str,
        lat: float,
        lng: float,
        radius_meters: int = 1500,
        limit: int = config.MAX_RESULTS,
    ) -> list[dict]:
        """Find places of ``category`` near a coordinate via Nearby Search.

        See :meth:`PlacesProvider.find_nearby` for the contract. Known
        categories map to a Places type; anything else falls back to a
        keyword search so new categories keep working.
        """
        category = require_non_empty_string(category, "category")
        _validate_coords(lat, lng)
        radius_meters = require_positive_int(radius_meters, "radius_meters")
        if radius_meters > MAX_NEARBY_RADIUS_METERS:
            raise ValueError(
                f"radius_meters must not exceed {MAX_NEARBY_RADIUS_METERS}."
            )
        limit = require_positive_int(limit, "limit")

        cache_key = (
            f"nearby||{category.lower()}"
            f"||{float(lat):.5f}||{float(lng):.5f}"
            f"||{radius_meters}||{limit}"
        )
        cached = cache_lookup(_places_cache, cache_key)
        if cached is not None:
            logger.info("places cache hit for nearby=%r", category)
            return cached

        place_type = _GOOGLE_PLACE_TYPES.get(category.lower())
        client = _get_client()
        logger.info(
            "places nearby: category=%r lat=%s lng=%s radius=%s",
            category,
            lat,
            lng,
            radius_meters,
        )
        try:
            if place_type is not None:
                response = client.places_nearby(
                    location=(float(lat), float(lng)),
                    radius=radius_meters,
                    type=place_type,
                )
            else:
                response = client.places_nearby(
                    location=(float(lat), float(lng)),
                    radius=radius_meters,
                    keyword=category,
                )
        except ApiError as exc:
            raise_for_google_api_error(
                exc,
                operation="Nearby",
                no_results_message=f"No {category!r} found nearby.",
            )
        except (Timeout, TransportError) as exc:
            raise_for_google_transport_error(exc, operation="Nearby")

        results = (response or {}).get("results", [])
        status = (response or {}).get("status", "OK")
        if status == "ZERO_RESULTS" or not results:
            raise NoResultsFoundError(f"No {category!r} found nearby.")

        normalized = _normalize_items(results, limit)
        return cache_store(_places_cache, cache_key, normalized, CACHE_TTL_SECONDS)

    def get_directions(self, origin: str, destination_place_id: str) -> dict:
        """Get a route summary via the Directions API.

        See :meth:`PlacesProvider.get_directions` for the contract.
        """
        origin = require_non_empty_string(origin, "origin")
        destination_place_id = require_non_empty_string(
            destination_place_id, "destination_place_id"
        )

        client = _get_client()
        destination = f"place_id:{destination_place_id}"
        logger.info("directions request: origin=%r", origin)
        try:
            routes = client.directions(origin, destination)
        except ApiError as exc:
            # Directions has no ZERO_RESULTS mapping: an unroutable
            # pair surfaces as an empty routes list below.
            raise_for_google_api_error(
                exc, operation="directions", map_zero_results=False
            )
        except (Timeout, TransportError) as exc:
            raise_for_google_transport_error(exc, operation="Directions")

        if not routes:
            raise NoResultsFoundError(
                "No route found for the given origin/destination."
            )

        leg = (routes[0].get("legs") or [{}])[0]
        distance = (leg.get("distance") or {}).get("text", "unknown distance")
        duration = (leg.get("duration") or {}).get("text", "unknown duration")
        return {
            "distance": distance,
            "duration": duration,
            "origin": origin,
            "destination_place_id": destination_place_id,
        }
