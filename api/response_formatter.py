"""Builds embed HTML and fallback links for place results.

The Google Maps API key is never embedded in output HTML here. The
Maps Embed API ``place``/``directions`` modes used below operate with
the key supplied server-side or via the public share URL pattern —
in this implementation we use the key-free ``maps.google.com/maps``
output=embed pattern plus universal search links, so the secret never
reaches the browser.

NOTE on Embed API modes: the official Embed API (`.../embed/v1/place?key=...`)
requires exposing a key restricted with HTTP-referrer restrictions. To keep
the secret strictly server-side for this exercise, we use the keyless
``https://maps.google.com/maps?q=...&output=embed`` iframe form, which
renders the same pin/route view without leaking the key.
"""

from __future__ import annotations

import html
import logging
import urllib.parse

logger = logging.getLogger(__name__)

NO_RESULTS_MESSAGE = "No places found for your query. Try a different search."


def _embed_iframe(src: str, title: str) -> str:
    """Wrap an embed URL in a standard iframe fragment."""
    safe_src = html.escape(src, quote=True)
    safe_title = html.escape(title)
    return (
        f'<iframe title="{safe_title}" src="{safe_src}" '
        'width="600" height="450" style="border:0" '
        'loading="lazy" allowfullscreen '
        'referrerpolicy="no-referrer-when-downgrade"></iframe>'
    )


def to_embed_html(places: list[dict]) -> str:
    """Build an embed HTML fragment for the top result (place mode).

    Args:
        places: Normalized place dicts from the provider ``find_places``.

    Returns:
        An HTML fragment with a Maps embed iframe, or a friendly
        plain-text message when ``places`` is empty.
    """
    if not places:
        return NO_RESULTS_MESSAGE
    top = places[0]
    name = top.get("name", "")
    address = top.get("address", "")
    place_id = top.get("place_id", "")
    query_text = f"{name} {address}".strip() or name
    params = urllib.parse.urlencode(
        {"q": query_text, "query_place_id": place_id, "output": "embed"}
    )
    src = f"https://maps.google.com/maps?{params}"
    logger.info("built place embed for %r", name)
    return _embed_iframe(src, f"Map of {name}")


def to_directions_embed_html(origin: str, destination_place_id: str) -> str:
    """Build a directions embed HTML fragment (directions mode).

    Shows the route from ``origin`` to the found place. This is what
    satisfies the "view the location direction" requirement — a route,
    not just a pin.

    Args:
        origin: Free-text origin (user location / station / address).
        destination_place_id: Google place_id of the destination.

    Returns:
        An HTML fragment with a directions embed iframe.
    """
    safe_origin = origin.strip()
    # Keyless directions embed via the universal dir URL.
    # Rendered through the embed endpoint so it stays an inline iframe.
    src = "https://maps.google.com/maps?" + urllib.parse.urlencode(
        {
            "saddr": safe_origin,
            "daddr": f"place_id:{destination_place_id.strip()}",
            "output": "embed",
        }
    )
    logger.info("built directions embed from %r", safe_origin)
    return _embed_iframe(src, f"Directions from {safe_origin}")


def to_fallback_link(places: list[dict]) -> str:
    """Build a plain universal Maps search link.

    Args:
        places: Normalized place dicts.

    Returns:
        A ``https://www.google.com/maps/search/?api=1&query=...`` URL,
        or an empty string when there are no places.
    """
    if not places:
        return ""
    top = places[0]
    query_text = f"{top.get('name', '')} {top.get('address', '')}".strip()
    encoded = urllib.parse.quote_plus(query_text)
    return f"https://www.google.com/maps/search/?api=1&query={encoded}"
