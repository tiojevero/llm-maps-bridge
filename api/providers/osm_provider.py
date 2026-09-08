"""Free, keyless places provider backed by OpenStreetMap services.

Search uses the Nominatim API (no key, no billing account required),
category/proximity search uses the Overpass API (Nominatim's own docs
say it is not reliable for "all objects of a type in an area" and
recommend Overpass instead), and routing uses the OSRM public demo
server, since Nominatim itself does not do routing. Results use the
same normalized shape as the Google provider so the API layer stays
provider-agnostic.

IMPORTANT — free public instances, light development use only:
Nominatim's usage policy requires a descriptive User-Agent (sent on
every request below), at most 1 request per second (enforced by the
rate gate here), and no bulk/heavy traffic. Overpass and OSRM are
likewise shared community servers, so the same gate applies before
their calls too. For production, self-host Nominatim/OSRM/Overpass or
switch to a paid geocoding provider (e.g. set ``MAPS_PROVIDER=google``).
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse

import httpx

from api import config
from api.providers._common import (
    CacheEntry as _CommonCacheEntry,
    cache_lookup,
    cache_store,
    check_http_status,
    fetch_json,
    normalize_query as _normalize_query_common,
    require_non_empty_string,
    require_positive_int,
    validate_coords as _validate_coords_common,
)
from api.providers.base import (
    NoResultsFoundError,
    PlacesError,
    PlacesProvider,
)

logger = logging.getLogger(__name__)

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSRM_ROUTE_URL = "https://router.project-osrm.org/route/v1/driving"

# Maps natural-language categories to OSM tags for Overpass queries.
# Deliberately small — a handful of common cases. Extend this dict
# later rather than trying to be exhaustive; unmapped categories fall
# back to a best-guess ``amenity=<category>`` tag.
CATEGORY_TO_OSM_TAG = {
    "restaurant": "amenity=restaurant",
    "food": "amenity=restaurant",
    "cafe": "amenity=cafe",
    "coffee": "amenity=cafe",
    "pharmacy": "amenity=pharmacy",
    "drugstore": "amenity=pharmacy",
    "hospital": "amenity=hospital",
    "clinic": "amenity=clinic",
    "atm": "amenity=atm",
    "bank": "amenity=bank",
    "gas station": "amenity=fuel",
    "fuel": "amenity=fuel",
    "petrol": "amenity=fuel",
    "hotel": "tourism=hotel",
    "bar": "amenity=bar",
    "fast food": "amenity=fast_food",
    "supermarket": "shop=supermarket",
    "grocery": "shop=supermarket",
    "parking": "amenity=parking",
}

# Required by the Nominatim usage policy. Replace the contact with your
# own email or GitHub profile when deploying this anywhere.
USER_AGENT = "llm-places-finder/1.0 (contact: your-email-or-github)"

# Nominatim allows at most 1 request/second on the public instance.
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0

CACHE_TTL_SECONDS = 5 * 60


# Search cache (same 5-minute TTL approach as the Google provider) plus
# a registry mapping returned place_ids back to coordinates, which
# get_directions needs to build the OSRM request.
# The entry type lives in _common; this alias keeps existing imports working.
_CacheEntry = _CommonCacheEntry
_places_cache: dict[str, _CacheEntry] = {}
_coords_registry: dict[str, tuple[float, float, str]] = {}

_gate_lock = threading.Lock()
_last_request_ts = 0.0


def _respect_rate_limit() -> None:
    """Block until at least 1 second passed since the last upstream call."""
    global _last_request_ts
    with _gate_lock:
        now = time.monotonic()
        wait = NOMINATIM_MIN_INTERVAL_SECONDS - (now - _last_request_ts)
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_request_ts = now


def _normalize_query(query: str, location_bias: str | None) -> str:
    """Normalize a query+bias pair into a stable cache key."""
    return _normalize_query_common(query, location_bias)


def _http_get(url: str, params: dict) -> httpx.Response:
    """GET ``url`` with the policy-required User-Agent header."""
    return httpx.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=config.MAPS_REQUEST_TIMEOUT_SECONDS,
    )


def _http_post(url: str, data: dict) -> httpx.Response:
    """POST ``url`` with the policy-required User-Agent header."""
    return httpx.post(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT},
        timeout=config.MAPS_REQUEST_TIMEOUT_SECONDS,
    )


def _validate_coords(lat: float, lng: float) -> None:
    """Raise ValueError unless lat/lng are in valid ranges."""
    _validate_coords_common(lat, lng)


def _category_tag(category: str) -> str:
    """Map a natural-language category to an OSM ``key=value`` tag."""
    normalized = " ".join(category.strip().lower().split())
    if normalized in CATEGORY_TO_OSM_TAG:
        return CATEGORY_TO_OSM_TAG[normalized]
    # Best guess: most searchable POIs live under amenity=*.
    return f"amenity={normalized.replace(' ', '_')}"


def _build_overpass_query(tag: str, lat: float, lng: float, radius: int) -> str:
    """Build an Overpass QL query for ``tag`` around a point."""
    key, _, value = tag.partition("=")
    around = f"(around:{radius},{float(lat)},{float(lng)})"
    return (
        "[out:json][timeout:25];("
        f'node["{key}"="{value}"]{around};'
        f'way["{key}"="{value}"]{around};'
        f'relation["{key}"="{value}"]{around};'
        ");out center;"
    )


def _build_maps_url(lat: float, lon: float) -> str:
    """Build an OpenStreetMap link centered on the given coordinates."""
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"


def _check_response(response: httpx.Response, service: str) -> None:
    """Map upstream HTTP errors to provider exceptions.

    Thin alias over :func:`_common.check_http_status` kept so existing
    imports keep working.
    """
    check_http_status(response, service)


def _nominatim_search(search_text: str, limit: int) -> list[dict]:
    """Run one Nominatim search, returning the raw JSON list.

    Raises:
        MapsNetworkError: On network/timeout failures.
        QuotaExceededError: On HTTP 429.
        PlacesError: On other HTTP errors or undecodable responses.
    """
    _respect_rate_limit()
    data = fetch_json(
        "Nominatim",
        lambda: _http_get(
            NOMINATIM_SEARCH_URL,
            params={
                "q": search_text,
                "format": "jsonv2",
                "addressdetails": 1,
                "limit": limit,
            },
        ),
    )
    if not isinstance(data, list):
        logger.error("Nominatim returned a non-list payload")
        raise PlacesError("Nominatim returned an unexpected response.")
    return data


def _format_distance(meters: float) -> str:
    """Format OSRM meters like Google's "2.1 km" style."""
    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{int(meters)} m"


def _normalize_nominatim_items(items: list[dict], limit: int) -> list[dict]:
    """Normalize raw Nominatim items to the shared result shape.

    ``place_id`` combines ``osm_type`` and ``osm_id`` (e.g.
    ``"node/12345"``) so ids are unambiguous across OSM element types.
    Every normalized place is registered for later directions lookups.
    """
    normalized: list[dict] = []
    for item in items[:limit]:
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping Nominatim result without coords: %r", item)
            continue
        display_name = item.get("display_name", "Unknown place")
        # display_name is "Venue, street, city, ..."; the first
        # component is usually the venue name for POI searches.
        name = display_name.split(",")[0].strip() or display_name
        place_id = f"{item.get('osm_type', 'node')}/{item.get('osm_id', '')}"
        _coords_registry[place_id] = (lat, lon, display_name)
        normalized.append(
            {
                "name": name,
                "address": display_name,
                "lat": lat,
                "lng": lon,
                "place_id": place_id,
                "maps_url": _build_maps_url(lat, lon),
            }
        )
    return normalized


def _normalize_overpass_elements(
    elements: list[dict], category: str, limit: int
) -> list[dict]:
    """Normalize raw Overpass elements to the shared result shape."""
    normalized: list[dict] = []
    for el in elements[:limit]:
        el_type = el.get("type", "node")
        center = el.get("center", {}) if el_type != "node" else el
        try:
            lat = float(center["lat"])
            lon = float(center["lon"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping Overpass element without coords: %r", el)
            continue
        tags = el.get("tags") or {}
        name = tags.get("name") or f"{category.strip().title()} {el.get('id', '')}"
        address_parts = []
        street = " ".join(
            part
            for part in [tags.get("addr:housenumber"), tags.get("addr:street")]
            if part
        )
        if street:
            address_parts.append(street)
        city = tags.get("addr:city") or tags.get("addr:suburb")
        if city:
            address_parts.append(city)
        address = ", ".join(address_parts) or f"{lat:.5f}, {lon:.5f}"
        place_id = f"{el_type}/{el.get('id', '')}"
        _coords_registry[place_id] = (lat, lon, address)
        normalized.append(
            {
                "name": name.strip(),
                "address": address,
                "lat": lat,
                "lng": lon,
                "place_id": place_id,
                "maps_url": _build_maps_url(lat, lon),
            }
        )
    return normalized


def _format_duration(seconds: float) -> str:
    """Format OSRM seconds like Google's "9 mins" style."""
    minutes = int(round(seconds / 60))
    if minutes == 1:
        return "1 min"
    return f"{minutes} mins"


# OSRM maneuver types mapped to plain-language verbs. Deliberately
# small — unknown types fall back to a title-cased version of the raw
# type string, so new maneuver types still produce readable output.
_OSRM_MANEUVER_VERBS = {
    "depart": "Head",
    "arrive": "Arrive",
    "turn": "Turn",
    "new name": "Continue",
    "merge": "Merge",
    "roundabout": "Enter the roundabout",
    "rotary": "Enter the rotary",
    "exit roundabout": "Exit the roundabout",
    "exit rotary": "Exit the rotary",
}


def _format_osrm_step(step: dict) -> str:
    """Format one OSRM route step as a plain-language instruction."""
    maneuver = step.get("maneuver") or {}
    step_type = str(maneuver.get("type", "") or "").lower()
    modifier = str(maneuver.get("modifier", "") or "").strip()
    street = str(step.get("name", "") or "").strip()
    try:
        distance_m = int(float(step.get("distance", 0)))
    except (TypeError, ValueError):
        distance_m = 0

    if step_type == "arrive":
        return f"Arrive at {street}" if street else "Arrive at your destination"
    verb = _OSRM_MANEUVER_VERBS.get(
        step_type, step_type.replace("_", " ").title() or "Continue"
    )
    parts = verb
    if modifier and step_type in ("turn", "merge"):
        parts += f" {modifier}"
    if street:
        parts += f" onto {street}"
    if distance_m > 0:
        parts += f" for {distance_m}m"
    return parts


def clear_cache() -> None:
    """Clear the search cache and coords registry (useful in tests)."""
    _places_cache.clear()
    _coords_registry.clear()


class OsmProvider(PlacesProvider):
    """Places provider backed by free OSM services (no key, no billing)."""

    def find_places(
        self,
        query: str,
        location_bias: str | None = None,
        limit: int = config.MAX_RESULTS,
    ) -> list[dict]:
        """Find places via Nominatim search, normalized and cached.

        See :meth:`PlacesProvider.find_places` for the contract.
        ``place_id`` combines ``osm_type`` and ``osm_id``.
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
            # Comma form ("sushi, Malang"), not Google-style "sushi in
            # Malang": Nominatim parses the comma as a region qualifier
            # and returns far better matches (verified live; the "in"
            # form often yields zero results).
            search_text = f"{search_text}, {location_bias.strip()}"

        logger.info("OSM places search: query=%r", search_text)
        items = _nominatim_search(search_text, limit)

        normalized = _normalize_nominatim_items(items, limit)
        if not normalized:
            raise NoResultsFoundError(f"No places found for {search_text!r}.")

        return cache_store(_places_cache, cache_key, normalized, CACHE_TTL_SECONDS)

    def find_nearby(
        self,
        category: str,
        lat: float,
        lng: float,
        radius_meters: int = 1500,
        limit: int = config.MAX_RESULTS,
    ) -> list[dict]:
        """Find places of ``category`` nearby via the Overpass API.

        See :meth:`PlacesProvider.find_nearby` for the contract.
        Nominatim itself is unreliable for "all objects of a type in an
        area", so Overpass is used instead, per Nominatim's own docs.
        """
        category = require_non_empty_string(category, "category")
        _validate_coords(lat, lng)
        radius_meters = require_positive_int(radius_meters, "radius_meters")
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

        tag = _category_tag(category)
        query = _build_overpass_query(tag, float(lat), float(lng), radius_meters)
        logger.info("OSM nearby search: category=%r tag=%r", category, tag)
        _respect_rate_limit()
        data = fetch_json(
            "Overpass", lambda: _http_post(OVERPASS_URL, data={"data": query})
        )

        elements = (data or {}).get("elements", [])
        normalized = _normalize_overpass_elements(elements, category, limit)
        if not normalized:
            raise NoResultsFoundError(
                f"No {category!r} found within {radius_meters} m."
            )

        return cache_store(_places_cache, cache_key, normalized, CACHE_TTL_SECONDS)

    def get_directions(self, origin: str, destination_place_id: str) -> dict:
        """Route via OSRM, geocoding the origin with Nominatim first.

        See :meth:`PlacesProvider.get_directions` for the contract.
        The destination is resolved from places returned earlier in this
        process; unknown ids raise :class:`NoResultsFoundError`.
        """
        origin = require_non_empty_string(origin, "origin")
        destination_place_id = require_non_empty_string(
            destination_place_id, "destination_place_id"
        )

        # Tolerate URL-encoded ids (e.g. "node%2F12345" scraped from an
        # embed URL): the registry keys are the raw "type/id" form.
        destination_place_id = urllib.parse.unquote(destination_place_id)
        dest = _coords_registry.get(destination_place_id)
        if dest is None:
            raise NoResultsFoundError(
                "Unknown destination. Search for the place again first, "
                "then request directions with its place_id."
            )
        dest_lat, dest_lon, _ = dest

        logger.info("OSM directions request: origin=%r", origin)
        origin_items = _nominatim_search(origin, 1)
        if not origin_items:
            raise NoResultsFoundError(f"Could not geocode origin {origin!r}.")
        try:
            origin_lat = float(origin_items[0]["lat"])
            origin_lon = float(origin_items[0]["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NoResultsFoundError(
                f"Could not geocode origin {origin!r}."
            ) from exc

        _respect_rate_limit()
        route_url = f"{OSRM_ROUTE_URL}/{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
        data = fetch_json(
            "OSRM",
            lambda: _http_get(
                route_url, params={"overview": "false", "steps": "true"}
            ),
        )

        routes = (data or {}).get("routes", [])
        if (data or {}).get("code") != "Ok" or not routes:
            raise NoResultsFoundError(
                "No route found for the given origin/destination."
            )

        steps: list[str] = []
        for leg in routes[0].get("legs", []):
            for step in leg.get("steps", []):
                steps.append(_format_osrm_step(step))

        return {
            "distance": _format_distance(float(routes[0].get("distance", 0))),
            "duration": _format_duration(float(routes[0].get("duration", 0))),
            "origin": origin,
            "destination_place_id": destination_place_id,
            "steps": steps,
        }
