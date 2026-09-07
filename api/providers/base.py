"""Provider interface and shared error types for place lookups.

Every provider returns the same normalized shapes so the API layer
never branches on which provider is active:

- ``find_places`` / ``find_nearby`` -> ``list[dict]`` with keys
  ``name, address, lat, lng, place_id, maps_url``.
- ``get_directions`` -> ``dict`` with keys
  ``distance, duration, origin, destination_place_id`` (the distance /
  duration core both providers produce; the API layer derives the
  embeddable map from origin + destination).

Exception naming: the canonical shared set is
``NoResultsFoundError``, ``ProviderRateLimitedError`` and
``ProviderUnavailableError`` (all under :class:`PlacesError`).
``QuotaExceededError`` and ``MapsNetworkError`` are kept as subclasses
of those for backwards compatibility — catching either the old or the
new name works.
"""

from __future__ import annotations

import abc

from api import config


class PlacesError(Exception):
    """Base class for all places-lookup failures."""


class NoResultsFoundError(PlacesError):
    """Raised when the provider returns zero results or no route."""


class ProviderRateLimitedError(PlacesError):
    """Raised when the provider reports quota/rate limiting.

    Google reports this as ``OVER_QUERY_LIMIT``; for keyless providers
    an HTTP 429 from the upstream service maps here too.
    """


class QuotaExceededError(ProviderRateLimitedError):
    """Alias kept for backwards compatibility (see base docstring)."""


class ProviderUnavailableError(PlacesError):
    """Raised on network failures, timeouts, or upstream HTTP errors."""


class MapsNetworkError(ProviderUnavailableError):
    """Alias kept for backwards compatibility (see base docstring)."""


class InvalidApiKeyError(PlacesError):
    """Raised when the provider rejects the credentials (``REQUEST_DENIED``).

    Only applies to keyed providers such as Google Maps.
    """


class PlacesProvider(abc.ABC):
    """Abstract interface every places provider must implement."""

    @abc.abstractmethod
    def find_places(
        self,
        query: str,
        location_bias: str | None = None,
        limit: int = config.MAX_RESULTS,
    ) -> list[dict]:
        """Find places matching ``query``, optionally biased to a region.

        Args:
            query: Free-text query, e.g. "sushi near me".
            location_bias: Optional region/bias, e.g. "Malang".
            limit: Maximum number of results to return.

        Returns:
            List of dicts with keys
            ``name, address, lat, lng, place_id, maps_url``.

        Raises:
            ValueError: If ``query`` is empty or ``limit`` is not positive.
            NoResultsFoundError: If the provider returns zero results.
            ProviderRateLimitedError: On provider quota/rate limiting.
            InvalidApiKeyError: On rejected credentials (keyed providers).
            ProviderUnavailableError: On network/timeout failures.
            PlacesError: On other provider errors.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def find_nearby(
        self,
        category: str,
        lat: float,
        lng: float,
        radius_meters: int = 1500,
        limit: int = config.MAX_RESULTS,
    ) -> list[dict]:
        """Find places of ``category`` near a coordinate.

        This answers "places to eat near me", distinct from the
        name/address search of :meth:`find_places`.

        Args:
            category: Natural-language category, e.g. "restaurant".
            lat: Center latitude (-90..90).
            lng: Center longitude (-180..180).
            radius_meters: Search radius in meters (must be positive).
            limit: Maximum number of results to return.

        Returns:
            List of dicts with keys
            ``name, address, lat, lng, place_id, maps_url``.

        Raises:
            ValueError: If inputs are invalid.
            NoResultsFoundError: If the provider returns zero results.
            ProviderRateLimitedError: On provider quota/rate limiting.
            InvalidApiKeyError: On rejected credentials (keyed providers).
            ProviderUnavailableError: On network/timeout failures.
            PlacesError: On other provider errors.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_directions(self, origin: str, destination_place_id: str) -> dict:
        """Get a route summary from ``origin`` to a previously found place.

        Args:
            origin: Free-text origin, e.g. "Malang station".
            destination_place_id: Provider place id from a ``find_places``
                or ``find_nearby`` result.

        Returns:
            Dict with ``distance, duration, origin, destination_place_id``.

        Raises:
            ValueError: If inputs are empty.
            NoResultsFoundError: If no route is found.
            ProviderRateLimitedError: On provider quota/rate limiting.
            InvalidApiKeyError: On rejected credentials (keyed providers).
            ProviderUnavailableError: On network/timeout failures.
            PlacesError: On other provider errors.
        """
        raise NotImplementedError
