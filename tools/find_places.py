"""Open WebUI Tool: thin HTTP client for the llm-places-finder backend.

This file contains NO Google Maps logic, NO API key, and NO business
logic — just a request/response passthrough to the standalone FastAPI
backend (default ``http://localhost:8000`` via ``BACKEND_API_URL``).

Install: copy this file into Open WebUI's Tools directory (or add via
the Admin UI > Tools), then enable it for your model.

Rich UI Embedding convention: the tool returns the backend's
``embed_html`` so Open WebUI renders the inline map iframe in chat.

NOTE: this file must stay self-contained (stdlib + httpx only) because
it is copied verbatim into Open WebUI — it cannot import from ``api/``.
All shared messages and backend-call handling therefore live as
module-level constants/helpers below, used by every Tool method.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

BACKEND_API_URL = os.getenv("BACKEND_API_URL", "http://localhost:8000").rstrip("/")
REQUEST_TIMEOUT_SECONDS = 20

# ---------------------------------------------------------------------------
# User-facing messages — each distinct string is defined exactly once.
# ---------------------------------------------------------------------------

_MSG_UNREACHABLE = (
    "The places service is currently unreachable. "
    "Please make sure the backend API is running and try again."
)
_MSG_TIMEOUT = "The places service timed out. Please try again in a moment."
_MSG_HTTP = "Could not reach the places service. Please try again later."
_MSG_RATE_LIMITED = "Too many requests right now — please wait a bit and try again."
_MSG_BACKEND_ERROR = "The places service returned an error. Please try again later."
_MSG_BAD_RESPONSE = "The places service returned an unexpected response."
_MSG_NO_RESULTS = "No places found. Try a different search."


# ---------------------------------------------------------------------------
# Shared backend-call handling — one implementation for all Tool methods.
# ---------------------------------------------------------------------------


def _post_backend(path: str, payload: dict) -> httpx.Response | str:
    """POST ``payload`` to the backend, mapping transport errors to messages.

    Returns the response on success, or a user-facing error string that
    the caller should return directly (checked via ``isinstance(..., str)``).
    """
    try:
        return httpx.post(
            f"{BACKEND_API_URL}{path}", json=payload, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.ConnectError:
        logger.error("backend unreachable at %s", BACKEND_API_URL)
        return _MSG_UNREACHABLE
    except httpx.TimeoutException:
        logger.error("backend request timed out")
        return _MSG_TIMEOUT
    except httpx.HTTPError as exc:
        logger.error("backend request failed: %s", type(exc).__name__)
        return _MSG_HTTP


def _response_detail(resp: httpx.Response, default: str) -> str:
    """Extract ``detail`` from a search/nearby error body.

    Falls back to the response text, then ``default`` — matching the
    original per-method handling exactly.
    """
    try:
        detail = resp.json().get("detail", default)
    except ValueError:
        detail = resp.text or default
    return detail if isinstance(detail, str) else default


def _directions_detail(resp: httpx.Response) -> str:
    """Extract ``detail`` from a directions error body (no text fallback)."""
    try:
        detail = resp.json().get("detail", "backend error")
    except ValueError:
        detail = "backend error"
    return detail if isinstance(detail, str) else "backend error"


def _status_error(resp: httpx.Response, *, invalid_prefix: str | None) -> str | None:
    """Map non-200 statuses to a user message, or ``None`` when OK.

    ``invalid_prefix`` labels 400s (e.g. ``"Invalid search request"``).
    When ``None`` (directions), every non-200 uses the ``"Could not get
    directions: ..."`` shape instead of a separate 400 branch.
    """
    if resp.status_code == 429:
        return _MSG_RATE_LIMITED
    if resp.status_code != 200:
        if invalid_prefix is None:
            return f"Could not get directions: {_directions_detail(resp)}"
        if resp.status_code == 400:
            return f"{invalid_prefix}: {_response_detail(resp, 'bad request')}"
        return _MSG_BACKEND_ERROR
    return None


def _decode_body(resp: httpx.Response) -> tuple[dict | None, str | None]:
    """Decode a 200 body, returning ``(data, None)`` or ``(None, message)``."""
    try:
        return resp.json(), None
    except ValueError:
        logger.error("backend returned non-JSON response")
        return None, _MSG_BAD_RESPONSE


def _render_places(data: dict, *, header_prefix: str) -> str:
    """Render a search/nearby payload as header + embed + fallback link.

    Each result is listed with its raw ``place_id`` in plain text. This
    matters: the model must copy that literal into ``get_directions``,
    and the only other place the id appears (URL-encoded inside the
    iframe ``src``) is not safe to copy.
    """
    embed_html = data.get("embed_html", "")
    fallback_link = data.get("fallback_link", "")
    results = data.get("results", [])

    if not results:
        # Backend returns a friendly plain-text message in embed_html
        # when nothing was found.
        return embed_html or _MSG_NO_RESULTS

    items = "".join(
        f"<li>{r.get('name', '?')} — place_id: {r.get('place_id', '')}</li>"
        for r in results[:5]
    )
    suffix = (
        f'<p><a href="{fallback_link}" target="_blank">Open in Google Maps</a></p>'
        if fallback_link
        else ""
    )
    header = f"<p>{header_prefix}:</p><ol>{items}</ol>" if items else ""
    # Returned with Content-Disposition: inline by Open WebUI's
    # HTMLResponse wrapper so the iframe renders in chat.
    return f"{header}{embed_html}{suffix}"


class Tools:
    """Find real places and render them as an embedded map in chat."""

    def find_places(self, query: str, near: str | None = None) -> str:
        """Search for real places and return an inline map embed.

        Use this when the user asks for a location, e.g. "sushi near me",
        "nearest pharmacy in Malang", or "ramen in Jakarta".

        :param query: Free-text place query, e.g. "sushi" or "pharmacy".
        :param near: Optional location bias, e.g. "Malang".
        :return: HTML string with an embedded map iframe (rendered inline
            in chat), or a clear error/fallback message.
        """
        if not query or not query.strip():
            return "Please provide a non-empty place query, e.g. 'sushi in Malang'."

        payload: dict[str, str | None] = {"query": query.strip()}
        if near and near.strip():
            payload["near"] = near.strip()

        resp = _post_backend("/places/search", payload)
        if isinstance(resp, str):
            return resp
        if (error := _status_error(resp, invalid_prefix="Invalid search request")):
            return error
        data, decode_error = _decode_body(resp)
        if decode_error is not None:
            return decode_error
        assert data is not None
        return _render_places(data, header_prefix="Found")

    def find_nearby(
        self, category: str, lat: float, lng: float, radius_meters: int = 1500
    ) -> str:
        """Find places of a category near a coordinate, as an inline map.

        Use this when the user asks for nearby amenities, e.g. "places
        to eat near me", "pharmacies around here", or "cafes near the
        station" (use the coordinates from a previous result).

        :param category: Place category, e.g. "restaurant" or "pharmacy".
        :param lat: Center latitude (-90..90).
        :param lng: Center longitude (-180..180).
        :param radius_meters: Search radius in meters (default 1500).
        :return: HTML string with an embedded map iframe (rendered inline
            in chat), or a clear error/fallback message.
        """
        if not category or not category.strip():
            return "Please provide a place category, e.g. 'restaurant'."

        resp = _post_backend(
            "/places/nearby",
            {
                "category": category.strip(),
                "lat": lat,
                "lng": lng,
                "radius_meters": radius_meters,
            },
        )
        if isinstance(resp, str):
            return resp
        if (error := _status_error(resp, invalid_prefix="Invalid nearby request")):
            return error
        data, decode_error = _decode_body(resp)
        if decode_error is not None:
            return decode_error
        assert data is not None
        return _render_places(data, header_prefix="Found nearby")

    def get_directions(self, origin: str, destination_place_id: str) -> str:
        """Show a route from an origin to a previously found place.

        Use this as a follow-up after find_places, when the user asks for
        directions, e.g. "how do I get there from Malang station?".

        :param origin: Route origin, e.g. "Malang station".
        :param destination_place_id: The place_id copied exactly as shown
            in the find_places/find_nearby result list (e.g. "node/12345").
            Copy it literally — do not take it from the map iframe URL,
            where it appears URL-encoded.
        :return: HTML string with an embedded directions iframe, or a
            clear error message.
        """
        if not origin or not origin.strip():
            return "Please provide a non-empty origin, e.g. 'Malang station'."
        if not destination_place_id or not destination_place_id.strip():
            return "Please provide the destination place_id from a search result."

        resp = _post_backend(
            "/places/directions",
            {
                "origin": origin.strip(),
                "destination_place_id": destination_place_id.strip(),
            },
        )
        if isinstance(resp, str):
            return resp
        if (error := _status_error(resp, invalid_prefix=None)):
            return error
        data, decode_error = _decode_body(resp)
        if decode_error is not None:
            return decode_error
        assert data is not None

        summary = f"<p>Distance: {data.get('distance', '?')} • Duration: {data.get('duration', '?')}</p>"
        return f"{summary}{data.get('directions_embed_html', '')}"
