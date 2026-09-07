"""Unit tests for api.response_formatter."""

from __future__ import annotations

from api import response_formatter

SAMPLE_PLACES = [
    {
        "name": "Sushi Place",
        "address": "Jl. Test No. 1, Malang",
        "lat": -7.98,
        "lng": 112.62,
        "place_id": "abc123",
        "maps_url": "https://www.google.com/maps/search/?api=1&query=Sushi",
    }
]


def test_to_embed_html_contains_iframe() -> None:
    """Top result produces an iframe embed fragment."""
    out = response_formatter.to_embed_html(SAMPLE_PLACES)
    assert "<iframe" in out
    assert "maps.google.com/maps" in out
    assert "output=embed" in out or "output%3Dembed" in out or "embed" in out


def test_to_embed_html_empty_returns_message() -> None:
    """Empty input returns a friendly message, not a broken embed."""
    out = response_formatter.to_embed_html([])
    assert out == response_formatter.NO_RESULTS_MESSAGE
    assert "<iframe" not in out


def test_to_directions_embed_html_contains_iframe() -> None:
    """Directions mode produces a route iframe fragment."""
    out = response_formatter.to_directions_embed_html("Malang station", "abc123")
    assert "<iframe" in out
    assert "Malang" in out or "saddr" in out


def test_to_fallback_link_format() -> None:
    """Fallback link uses the universal search URL scheme."""
    link = response_formatter.to_fallback_link(SAMPLE_PLACES)
    assert link.startswith("https://www.google.com/maps/search/?api=1&query=")


def test_to_fallback_link_empty() -> None:
    """Empty input yields an empty fallback link."""
    assert response_formatter.to_fallback_link([]) == ""
