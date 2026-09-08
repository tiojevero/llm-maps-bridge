"""Unit tests for the Open WebUI Tool rendering (no backend needed)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from find_places import _render_places


def test_render_lists_raw_place_ids() -> None:
    """Results expose copyable place_ids, not just names.

    Regression test: the model once scraped the URL-encoded id from the
    iframe src ("node%2F12345") because no raw id was shown in text.
    """
    html = _render_places(
        {
            "embed_html": "<iframe src='https://maps.google.com/maps?query_place_id=node%2F12345'></iframe>",
            "fallback_link": "https://example.test/",
            "results": [
                {
                    "name": "Sushi Place",
                    "address": "Jl. Test",
                    "lat": -7.98,
                    "lng": 112.62,
                    "place_id": "node/12345",
                    "maps_url": "https://example.test/",
                }
            ],
        },
        header_prefix="Found",
    )
    assert "place_id: node/12345" in html
    assert "Sushi Place" in html
