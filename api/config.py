"""Application configuration.

Loads and validates environment variables for the backend API.

The Google Maps API key lives ONLY here (server-side). It is never sent
to, logged by, or reachable from the Open WebUI Tool or the browser.

Google Cloud hardening (manual steps in the Cloud Console, not enforced
by code):
  - Restrict the key via API restrictions to Places API, Geocoding API,
    Directions API, and Maps Embed API only.
  - Add an IP restriction to this server's IP.
  - Set a budget alert and/or daily quota cap.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from api.providers.base import PlacesProvider

logger = logging.getLogger(__name__)

load_dotenv()


def _get_int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on missing/invalid."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Invalid int for %s=%r; using default %d", name, raw, default)
        return default


def _get_str_env(name: str, default: str) -> str:
    """Read a str env var, falling back to ``default`` when unset/blank."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _get_origins_env(name: str, default: str) -> list[str]:
    """Parse a comma-separated origins env var into a list."""
    raw = _get_str_env(name, default)
    return [o.strip() for o in raw.split(",") if o.strip()]


GOOGLE_MAPS_API_KEY: str | None = os.getenv("GOOGLE_MAPS_API_KEY") or None

MAPS_REQUEST_TIMEOUT_SECONDS: int = _get_int_env("MAPS_REQUEST_TIMEOUT_SECONDS", 10)
MAX_RESULTS: int = _get_int_env("MAX_RESULTS", 5)

RATE_LIMIT_REQUESTS: int = _get_int_env("RATE_LIMIT_REQUESTS", 30)
RATE_LIMIT_WINDOW_SECONDS: int = _get_int_env("RATE_LIMIT_WINDOW_SECONDS", 60)

CORS_ALLOWED_ORIGINS: list[str] = _get_origins_env(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8080"
)

BACKEND_API_URL: str = _get_str_env("BACKEND_API_URL", "http://localhost:8000")


def require_google_maps_api_key() -> str:
    """Return the Google Maps API key or raise a descriptive error.

    Only needed when ``MAPS_PROVIDER=google``. OSM mode never calls this.

    Raises:
        RuntimeError: If ``GOOGLE_MAPS_API_KEY`` is missing or blank.

    Returns:
        The configured API key.
    """
    api_key = os.getenv("GOOGLE_MAPS_API_KEY") or GOOGLE_MAPS_API_KEY
    if not api_key:
        raise RuntimeError(
            "MAPS_PROVIDER=google is set but GOOGLE_MAPS_API_KEY is missing. "
            "Provide a valid restricted key (Places/Geocoding/Directions/ "
            "Maps Embed APIs only), or unset MAPS_PROVIDER to fall back to "
            "the free OSM option."
        )
    return api_key


def get_places_provider() -> PlacesProvider:
    """Return the configured places provider.

    Reads ``MAPS_PROVIDER`` live from the environment (``"osm"`` by
    default, ``"google"`` to switch). When ``"google"`` is selected the
    key is validated eagerly so a missing key fails fast with a clear
    error instead of silently falling back and hiding the
    misconfiguration. OSM mode never touches the Google key.

    Raises:
        ValueError: If ``MAPS_PROVIDER`` is not ``"osm"`` or ``"google"``.
        RuntimeError: If ``"google"`` is selected but the key is missing.

    Returns:
        The active :class:`PlacesProvider` implementation.
    """
    # Lazy imports: providers import this config module, so importing
    # them at top level would be circular.
    from api.providers.google_provider import GoogleMapsProvider
    from api.providers.osm_provider import OsmProvider

    name = (os.getenv("MAPS_PROVIDER", "osm") or "osm").strip().lower()
    if name == "google":
        require_google_maps_api_key()
        return GoogleMapsProvider()
    if name == "osm":
        return OsmProvider()
    raise ValueError(f"Unknown MAPS_PROVIDER={name!r}; expected 'osm' or 'google'.")
