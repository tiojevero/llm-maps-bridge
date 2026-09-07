"""Shared validation, caching, and error-mapping helpers for providers.

This module exists to keep ``google_provider`` and ``osm_provider`` free
of copy-pasted error handling: every Google ``ApiError`` status maps to
a typed exception in exactly one place, every upstream HTTP/JSON failure
maps in exactly one place, and input validation + TTL-cache boilerplate
is shared too.

Behavior is intentionally unchanged — these are pure extractions of the
logic that was previously duplicated across ``find_places``,
``find_nearby`` and ``get_directions``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from googlemaps.exceptions import ApiError, Timeout, TransportError

from api.providers.base import (
    InvalidApiKeyError,
    MapsNetworkError,
    NoResultsFoundError,
    PlacesError,
    QuotaExceededError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def require_non_empty_string(value: str | None, field: str) -> str:
    """Return ``value`` stripped, or raise ``ValueError`` when blank."""
    if not value or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def require_positive_int(value: int | None, field: str) -> int:
    """Return ``value`` as int, or raise ``ValueError`` when not positive."""
    try:
        as_int = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer.") from exc
    if as_int <= 0:
        raise ValueError(f"{field} must be a positive integer.")
    return as_int


def validate_coords(lat: float, lng: float) -> tuple[float, float]:
    """Validate lat/lng ranges, returning them as floats.

    Raises:
        ValueError: If coords are non-numeric or out of range.
    """
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError) as exc:
        raise ValueError("lat and lng must be numbers.") from exc
    if not -90 <= lat_f <= 90:
        raise ValueError("lat must be between -90 and 90.")
    if not -180 <= lng_f <= 180:
        raise ValueError("lng must be between -180 and 180.")
    return lat_f, lng_f


def normalize_query(query: str, location_bias: str | None) -> str:
    """Normalize a query+bias pair into a stable cache key."""
    q = " ".join(query.strip().lower().split())
    b = " ".join((location_bias or "").strip().lower().split())
    return f"{q}||{b}"


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    expires_at: float
    value: list[dict]


def cache_lookup(cache: dict[str, CacheEntry], key: str) -> list[dict] | None:
    """Return the cached value, evicting it when expired.

    Returns ``None`` on miss or expiry so callers have one branch.
    """
    cached = cache.get(key)
    if cached is None:
        return None
    if cached.expires_at > time.monotonic():
        return cached.value
    cache.pop(key, None)
    return None


def cache_store(
    cache: dict[str, CacheEntry], key: str, value: list[dict], ttl_seconds: int
) -> list[dict]:
    """Store ``value`` under ``key`` and return it (for ``return store(...)``)."""
    cache[key] = CacheEntry(expires_at=time.monotonic() + ttl_seconds, value=value)
    return value


# ---------------------------------------------------------------------------
# Google error mapping (one place for all three provider methods)
# ---------------------------------------------------------------------------


def raise_for_google_api_error(
    exc: ApiError, *, operation: str, no_results_message: str = "",
    map_zero_results: bool = True,
) -> None:
    """Map a Google ``ApiError`` status to the typed provider exception.

    Raises:
        QuotaExceededError: On ``OVER_QUERY_LIMIT``.
        InvalidApiKeyError: On ``REQUEST_DENIED``.
        NoResultsFoundError: On ``ZERO_RESULTS`` (unless disabled).
        PlacesError: On any other status.
    """
    status = getattr(exc, "status", "")
    if status == "OVER_QUERY_LIMIT":
        logger.warning("Google quota/rate limit hit on %s", operation)
        raise QuotaExceededError(
            "Google Maps quota exceeded (OVER_QUERY_LIMIT)."
        ) from exc
    if status == "REQUEST_DENIED":
        logger.error("Google denied %s request (REQUEST_DENIED); check key", operation)
        raise InvalidApiKeyError(
            "Google Maps request denied (REQUEST_DENIED). "
            "Check API key restrictions."
            if operation != "directions"
            else "Google Maps request denied (REQUEST_DENIED)."
        ) from exc
    if map_zero_results and status == "ZERO_RESULTS":
        raise NoResultsFoundError(no_results_message) from exc
    logger.error("Google %s API error: status=%s", operation, status)
    raise PlacesError(
        f"Google Maps error (status={status or 'unknown'})."
    ) from exc


def raise_for_google_transport_error(
    exc: Timeout | TransportError, *, operation: str
) -> None:
    """Map googlemaps transport errors to :class:`MapsNetworkError`."""
    logger.error("Network/timeout calling Google %s API", operation)
    raise MapsNetworkError(
        "Network error or timeout contacting Google Maps."
    ) from exc


# ---------------------------------------------------------------------------
# OSM / plain-HTTP error mapping (one place for Nominatim/Overpass/OSRM)
# ---------------------------------------------------------------------------


def check_http_status(response: httpx.Response, service: str) -> None:
    """Map upstream HTTP error statuses to provider exceptions.

    Raises:
        QuotaExceededError: On HTTP 429.
        MapsNetworkError: On HTTP 5xx.
        PlacesError: On other HTTP 4xx.
    """
    if response.status_code == 429:
        logger.warning("%s rate limited us (HTTP 429)", service)
        raise QuotaExceededError(f"{service} rate limit exceeded (HTTP 429).")
    if response.status_code >= 500:
        logger.error("%s unavailable: HTTP %d", service, response.status_code)
        raise MapsNetworkError(
            f"{service} unavailable (HTTP {response.status_code})."
        )
    if response.status_code >= 400:
        logger.error("%s error: HTTP %d", service, response.status_code)
        raise PlacesError(f"{service} error (HTTP {response.status_code}).")


def decode_json_body(response: httpx.Response, service: str) -> Any:
    """Decode ``response.json()``, raising ``PlacesError`` when undecodable."""
    try:
        return response.json()
    except ValueError as exc:
        logger.error("%s returned undecodable JSON", service)
        raise PlacesError(
            f"{service} returned an unexpected response."
        ) from exc


def fetch_json(
    service: str, caller: Callable[[], httpx.Response]
) -> Any:
    """Call ``caller``, map transport + HTTP + JSON errors, return the body.

    ``caller`` is a zero-arg closure around ``_http_get`` / ``_http_post``
    so tests can keep patching those functions directly. This replaces
    the identical try/except/check/decode block previously repeated in
    every OSM method.
    """
    try:
        response = caller()
    except httpx.TimeoutException as exc:
        logger.error("Timeout calling %s", service)
        raise MapsNetworkError(
            f"Network timeout contacting {service}."
        ) from exc
    except httpx.HTTPError as exc:
        logger.error("Network error calling %s", service)
        raise MapsNetworkError(
            f"Network error contacting {service}."
        ) from exc
    check_http_status(response, service)
    return decode_json_body(response, service)
