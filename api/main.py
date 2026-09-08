"""Standalone FastAPI backend for LLM-driven place search.

Core deliverable: independently testable with curl/Postman, no Open
WebUI required. All provider logic, key handling, caching, and rate
limiting live here — never in the Tool.

Endpoints:
    POST /places/search      search places -> embed HTML + results
    POST /places/nearby      category/proximity search -> embed HTML + results
    POST /places/directions  route from origin -> directions embed
    GET  /health              liveness check
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from api import config, rate_limiter, response_formatter
from api.providers.base import (
    InvalidApiKeyError,
    NoResultsFoundError,
    PlacesError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

tags_metadata = [
    {
        "name": "places",
        "description": "Place search and directions (OSM by default, Google Maps optional).",
    },
    {
        "name": "system",
        "description": "Liveness and service metadata.",
    },
]

app = FastAPI(
    title="LLM Places Finder API",
    description=(
        "Backend that lets a local LLM find real places via a pluggable "
        "places provider (OpenStreetMap by default; Google Maps Platform "
        "when configured). Called by the Open WebUI Tool on the model's "
        "behalf: the model decides when to search, this API does the "
        "lookup and returns an embeddable map plus normalized results."
    ),
    version="1.0.0",
    openapi_tags=tags_metadata,
)

# CORS: locked down to only the origin(s) that need to call this backend
# (e.g. Open WebUI on localhost during development). Never use "*" in
# production — the browser would otherwise let any site spend quota.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.middleware("http")(rate_limiter.rate_limit_middleware)


def _server_misconfig_response(exc: Exception) -> JSONResponse:
    """Return a 502 for server misconfiguration (bad provider/key)."""
    logger.error("Server misconfigured: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)}
    )


def _bad_request(detail: str) -> JSONResponse:
    """Return a 400 with ``detail`` — one shape for every input error."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"detail": detail}
    )


def _resolve_provider() -> tuple[Any, JSONResponse | None]:
    """Resolve the configured provider, or a 502 response when misconfigured.

    Returns a ``(provider, error_response)`` pair: on success the second
    element is ``None``; on ``ValueError``/``RuntimeError`` (unknown
    ``MAPS_PROVIDER`` or missing key) the first is ``None`` and the
    second is ready to return.
    """
    try:
        return config.get_places_provider(), None
    except (ValueError, RuntimeError) as exc:
        # Unknown MAPS_PROVIDER or missing Google key: server
        # misconfiguration, never a client error — and never silent.
        return None, _server_misconfig_response(exc)


def _provider_failure_response(exc: Exception, operation: str) -> JSONResponse:
    """Map provider-side failures (and late RuntimeError) to a 502."""
    if isinstance(exc, ProviderRateLimitedError):
        logger.warning("Provider quota exceeded on %s", operation)
    elif isinstance(exc, InvalidApiKeyError):
        logger.error("Invalid provider API key on %s", operation)
    elif isinstance(exc, RuntimeError):
        logger.error("Server misconfigured: %s", exc)
    else:
        logger.error(
            "Provider failure on %s: %s", operation, type(exc).__name__
        )
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)}
    )


def _empty_places_response(exc: Exception) -> dict[str, Any]:
    """Friendly 200 payload used when search/nearby finds nothing."""
    return {"embed_html": str(exc), "fallback_link": "", "results": []}


class SearchRequest(BaseModel):
    """Request body for POST /places/search."""

    model_config = {
        "json_schema_extra": {"examples": [{"query": "ramen", "near": "Malang"}]}
    }

    query: str = Field(..., description="Free-text place query")
    near: str | None = Field(
        default=None, description="Optional location bias, e.g. 'Malang'"
    )


class DirectionsRequest(BaseModel):
    """Request body for POST /places/directions."""

    model_config = {
        "json_schema_extra": {
            "examples": [{"origin": "Malang station", "destination_place_id": "abc123"}]
        }
    }

    origin: str = Field(..., description="Route origin, e.g. 'Malang station'")
    destination_place_id: str = Field(
        ..., description="Provider place_id from a prior search result"
    )


class NearbyRequest(BaseModel):
    """Request body for POST /places/nearby."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "category": "pharmacy",
                    "lat": -7.98,
                    "lng": 112.62,
                    "radius_meters": 1500,
                }
            ]
        }
    }

    category: str = Field(..., description="Place category, e.g. 'pharmacy'")
    lat: float = Field(..., description="Center latitude (-90..90)")
    lng: float = Field(..., description="Center longitude (-180..180)")
    radius_meters: int = Field(
        default=1500, description="Search radius in meters (1..50000)"
    )


class PlaceResult(BaseModel):
    """One normalized place from the provider lookup."""

    name: str = Field(..., description="Place name, e.g. 'Sushi Place'")
    address: str = Field(..., description="Formatted address")
    lat: float | None = Field(default=None, description="Latitude")
    lng: float | None = Field(default=None, description="Longitude")
    place_id: str = Field(
        ..., description="Provider place id (Google place_id or OSM osm_id)"
    )
    maps_url: str = Field(..., description="Map URL for the place")


class SearchResponse(BaseModel):
    """Success payload for POST /places/search."""

    embed_html: str = Field(..., description="Embeddable map iframe for the top result")
    fallback_link: str = Field(..., description="Plain Google Maps search link")
    results: list[PlaceResult] = Field(..., description="Normalized place results")


class DirectionsResponse(BaseModel):
    """Success payload for POST /places/directions."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "directions_embed_html": "<iframe ...></iframe>",
                    "distance": "2.1 km",
                    "duration": "9 mins",
                    "steps": [
                        "Head onto Jl. Merdeka for 300m",
                        "Turn right onto Jl. Sudirman for 150m",
                        "Arrive at your destination",
                    ],
                }
            ]
        }
    }

    directions_embed_html: str = Field(..., description="Embeddable route iframe")
    distance: str = Field(..., description="Route distance, e.g. '2.1 km'")
    duration: str = Field(..., description="Route duration, e.g. '9 mins'")
    steps: list[str] = Field(
        ..., description="Turn-by-turn instructions in route order"
    )


class HealthResponse(BaseModel):
    """Liveness payload for GET /health."""

    status: str = Field(..., description="Service status, always 'ok'")


class ErrorResponse(BaseModel):
    """Error payload for non-200 responses."""

    detail: str = Field(..., description="Human-readable error message")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Redirect the base URL to the Swagger UI docs."""
    return RedirectResponse(url="/docs")


@app.get(
    "/health",
    tags=["system"],
    response_model=HealthResponse,
    summary="Liveness check",
)
def health() -> dict[str, str]:
    """Return a basic liveness payload.

    Unthrottled by the rate limiter; used by the Docker HEALTHCHECK
    and by operators to confirm the service is up.
    """
    return {"status": "ok"}


@app.post(
    "/places/search",
    tags=["places"],
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Search places and return an embeddable map",
    responses={
        400: {"model": ErrorResponse, "description": "Empty or missing query."},
        429: {"model": ErrorResponse, "description": "Backend per-IP rate limit hit."},
        502: {
            "model": ErrorResponse,
            "description": "Provider failed, timed out, or the server key is misconfigured.",
        },
    },
)
def search_places(body: SearchRequest) -> dict[str, Any] | JSONResponse:
    """Search real places and return an inline map plus normalized results.

    Calls the configured provider (``MAPS_PROVIDER``: ``osm`` by default,
    ``google`` when set) with caching, then builds an embeddable map
    iframe for the top result. Returns a friendly plain-text message
    with empty results instead of a broken embed when nothing is found.
    """
    if not body.query or not body.query.strip():
        return _bad_request("query must be a non-empty string.")
    near = body.near.strip() if body.near and body.near.strip() else None
    provider, error_response = _resolve_provider()
    if error_response is not None:
        return error_response
    assert provider is not None
    try:
        places = provider.find_places(body.query.strip(), near)
    except ValueError as exc:
        return _bad_request(str(exc))
    except NoResultsFoundError as exc:
        logger.info("no results for query=%r", body.query)
        return _empty_places_response(exc)
    except (
        ProviderRateLimitedError,
        InvalidApiKeyError,
        ProviderUnavailableError,
        PlacesError,
        RuntimeError,
    ) as exc:
        return _provider_failure_response(exc, "search")

    embed_html = response_formatter.to_embed_html(places)
    fallback_link = response_formatter.to_fallback_link(places)
    logger.info("search ok: query=%r results=%d", body.query, len(places))
    return {"embed_html": embed_html, "fallback_link": fallback_link, "results": places}


@app.post(
    "/places/nearby",
    tags=["places"],
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Find places of a category near a coordinate",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid category/coords/radius."},
        429: {"model": ErrorResponse, "description": "Backend per-IP rate limit hit."},
        502: {
            "model": ErrorResponse,
            "description": "Provider failed, timed out, or the server key is misconfigured.",
        },
    },
)
def search_nearby(body: NearbyRequest) -> dict[str, Any] | JSONResponse:
    """Find nearby places of a category and return an inline map.

    Calls the configured provider's ``find_nearby`` (Google Nearby
    Search or Overpass, depending on ``MAPS_PROVIDER``) and returns the
    same shape as ``/places/search``. This answers "places to eat near
    me", distinct from a name/address search.
    """
    if not body.category or not body.category.strip():
        return _bad_request("category must be a non-empty string.")
    try:
        lat, lng = float(body.lat), float(body.lng)
    except (TypeError, ValueError):
        return _bad_request("lat and lng must be numbers.")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return _bad_request("lat must be -90..90 and lng -180..180.")
    try:
        radius = int(body.radius_meters)
    except (TypeError, ValueError):
        return _bad_request("radius_meters must be an integer.")
    if not 1 <= radius <= 50000:
        return _bad_request("radius_meters must be between 1 and 50000.")
    provider, error_response = _resolve_provider()
    if error_response is not None:
        return error_response
    assert provider is not None
    try:
        places = provider.find_nearby(body.category.strip(), lat, lng, radius)
    except ValueError as exc:
        return _bad_request(str(exc))
    except NoResultsFoundError as exc:
        logger.info("no nearby results for category=%r", body.category)
        return _empty_places_response(exc)
    except (
        ProviderRateLimitedError,
        InvalidApiKeyError,
        ProviderUnavailableError,
        PlacesError,
        RuntimeError,
    ) as exc:
        return _provider_failure_response(exc, "nearby search")

    embed_html = response_formatter.to_embed_html(places)
    fallback_link = response_formatter.to_fallback_link(places)
    logger.info("nearby ok: category=%r results=%d", body.category, len(places))
    return {"embed_html": embed_html, "fallback_link": fallback_link, "results": places}


@app.post(
    "/places/directions",
    tags=["places"],
    response_model=DirectionsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a route with an embeddable directions map",
    responses={
        400: {
            "model": ErrorResponse,
            "description": "Empty origin or destination_place_id.",
        },
        429: {"model": ErrorResponse, "description": "Backend per-IP rate limit hit."},
        502: {
            "model": ErrorResponse,
            "description": "No route found, or the provider failed or timed out.",
        },
    },
)
def get_directions(body: DirectionsRequest) -> dict[str, Any] | JSONResponse:
    """Return a route summary plus an embeddable directions map.

    Calls the configured provider for the route from ``origin`` to the
    place identified by ``destination_place_id`` (taken from a prior
    search result). This is what shows the user a route, not just a
    location pin.
    """
    if not body.origin or not body.origin.strip():
        return _bad_request("origin must be a non-empty string.")
    if not body.destination_place_id or not body.destination_place_id.strip():
        return _bad_request("destination_place_id must be a non-empty string.")
    provider, error_response = _resolve_provider()
    if error_response is not None:
        return error_response
    assert provider is not None
    try:
        route = provider.get_directions(
            body.origin.strip(), body.destination_place_id.strip()
        )
    except ValueError as exc:
        return _bad_request(str(exc))
    except (
        NoResultsFoundError,
        ProviderRateLimitedError,
        InvalidApiKeyError,
        ProviderUnavailableError,
        PlacesError,
        RuntimeError,
    ) as exc:
        return _provider_failure_response(exc, "directions")

    directions_embed_html = response_formatter.to_directions_embed_html(
        route["origin"], route["destination_place_id"]
    )
    return {
        "directions_embed_html": directions_embed_html,
        "distance": route["distance"],
        "duration": route["duration"],
        "steps": route["steps"],
    }
