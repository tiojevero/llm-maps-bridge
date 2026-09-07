"""Open WebUI Tool: thin HTTP client for the llm-places-finder backend.

This file contains NO Google Maps logic, NO API key, and NO business
logic — just a request/response passthrough to the standalone FastAPI
backend (default ``http://localhost:8000`` via ``BACKEND_API_URL``).

Install: copy this file into Open WebUI's Tools directory (or add via
the Admin UI > Tools), then enable it for your model.

Rich UI Embedding convention: the tool returns the backend's
``embed_html`` so Open WebUI renders the inline map iframe in chat.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

BACKEND_API_URL = os.getenv("BACKEND_API_URL", "http://localhost:8000").rstrip("/")
REQUEST_TIMEOUT_SECONDS = 20


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

        url = f"{BACKEND_API_URL}/places/search"
        try:
            resp = httpx.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.ConnectError:
            logger.error("backend unreachable at %s", BACKEND_API_URL)
            return (
                "The places service is currently unreachable. "
                "Please make sure the backend API is running and try again."
            )
        except httpx.TimeoutException:
            logger.error("backend request timed out")
            return "The places service timed out. Please try again in a moment."
        except httpx.HTTPError as exc:
            logger.error("backend request failed: %s", type(exc).__name__)
            return "Could not reach the places service. Please try again later."

        if resp.status_code == 429:
            return "Too many requests right now — please wait a bit and try again."
        if resp.status_code == 400:
            try:
                detail = resp.json().get("detail", "bad request")
            except ValueError:
                detail = resp.text or "bad request"
            return f"Invalid search request: {detail}"
        if resp.status_code != 200:
            return "The places service returned an error. Please try again later."

        try:
            data = resp.json()
        except ValueError:
            logger.error("backend returned non-JSON response")
            return "The places service returned an unexpected response."

        embed_html = data.get("embed_html", "")
        fallback_link = data.get("fallback_link", "")
        results = data.get("results", [])

        if not results:
            # Backend returns a friendly plain-text message in embed_html
            # when nothing was found.
            return embed_html or "No places found. Try a different search."

        names = ", ".join(r.get("name", "?") for r in results[:3])
        suffix = (
            f'<p><a href="{fallback_link}" target="_blank">Open in Google Maps</a></p>'
            if fallback_link
            else ""
        )
        header = f"<p>Found: {names}</p>" if names else ""
        # Returned with Content-Disposition: inline by Open WebUI's
        # HTMLResponse wrapper so the iframe renders in chat.
        return f"{header}{embed_html}{suffix}"

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

        url = f"{BACKEND_API_URL}/places/nearby"
        try:
            resp = httpx.post(
                url,
                json={
                    "category": category.strip(),
                    "lat": lat,
                    "lng": lng,
                    "radius_meters": radius_meters,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.ConnectError:
            logger.error("backend unreachable at %s", BACKEND_API_URL)
            return (
                "The places service is currently unreachable. "
                "Please make sure the backend API is running and try again."
            )
        except httpx.TimeoutException:
            logger.error("backend request timed out")
            return "The places service timed out. Please try again in a moment."
        except httpx.HTTPError as exc:
            logger.error("backend request failed: %s", type(exc).__name__)
            return "Could not reach the places service. Please try again later."

        if resp.status_code == 429:
            return "Too many requests right now — please wait a bit and try again."
        if resp.status_code == 400:
            try:
                detail = resp.json().get("detail", "bad request")
            except ValueError:
                detail = resp.text or "bad request"
            return f"Invalid nearby request: {detail}"
        if resp.status_code != 200:
            return "The places service returned an error. Please try again later."

        try:
            data = resp.json()
        except ValueError:
            logger.error("backend returned non-JSON response")
            return "The places service returned an unexpected response."

        embed_html = data.get("embed_html", "")
        fallback_link = data.get("fallback_link", "")
        results = data.get("results", [])

        if not results:
            return embed_html or "No places found. Try a different search."

        names = ", ".join(r.get("name", "?") for r in results[:3])
        suffix = (
            f'<p><a href="{fallback_link}" target="_blank">Open in Google Maps</a></p>'
            if fallback_link
            else ""
        )
        header = f"<p>Found nearby: {names}</p>" if names else ""
        return f"{header}{embed_html}{suffix}"

    def get_directions(self, origin: str, destination_place_id: str) -> str:
        """Show a route from an origin to a previously found place.

        Use this as a follow-up after find_places, when the user asks for
        directions, e.g. "how do I get there from Malang station?".

        :param origin: Route origin, e.g. "Malang station".
        :param destination_place_id: The place_id from a find_places result.
        :return: HTML string with an embedded directions iframe, or a
            clear error message.
        """
        if not origin or not origin.strip():
            return "Please provide a non-empty origin, e.g. 'Malang station'."
        if not destination_place_id or not destination_place_id.strip():
            return "Please provide the destination place_id from a search result."

        url = f"{BACKEND_API_URL}/places/directions"
        try:
            resp = httpx.post(
                url,
                json={
                    "origin": origin.strip(),
                    "destination_place_id": destination_place_id.strip(),
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.ConnectError:
            logger.error("backend unreachable at %s", BACKEND_API_URL)
            return (
                "The places service is currently unreachable. Please try again later."
            )
        except httpx.TimeoutException:
            return "The places service timed out. Please try again in a moment."
        except httpx.HTTPError as exc:
            logger.error("backend request failed: %s", type(exc).__name__)
            return "Could not reach the places service. Please try again later."

        if resp.status_code == 429:
            return "Too many requests right now — please wait a bit and try again."
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", "backend error")
            except ValueError:
                detail = "backend error"
            return f"Could not get directions: {detail}"

        try:
            data = resp.json()
        except ValueError:
            return "The places service returned an unexpected response."

        summary = f"<p>Distance: {data.get('distance', '?')} • Duration: {data.get('duration', '?')}</p>"
        return f"{summary}{data.get('directions_embed_html', '')}"
