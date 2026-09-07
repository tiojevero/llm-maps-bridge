"""Simple per-IP sliding-window rate limiter for the backend API.

This is the layer that actually protects the Google Maps quota, since
the Open WebUI Tool is only a passthrough. Rate limiting in the Tool
alone would be bypassable; enforcing it here covers every caller.

Scope: in-memory, per-process, per-IP. Enough for this single-instance
test service; a multi-instance deployment would need Redis (see
DECISIONS.md).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from api import config

logger = logging.getLogger(__name__)

# client_ip -> deque of request timestamps (monotonic seconds)
_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """Resolve the client IP, preferring X-Forwarded-For when present."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


async def rate_limit_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """FastAPI middleware enforcing the per-IP sliding window.

    Args:
        request: The incoming request.
        call_next: The next handler in the middleware chain.

    Returns:
        The downstream response, or 429 JSON when over the limit.
    """
    # Only throttle the billable/upstream endpoints; leave /health unthrottled.
    if request.url.path not in (
        "/places/search",
        "/places/nearby",
        "/places/directions",
    ):
        return await call_next(request)

    limit = config.RATE_LIMIT_REQUESTS
    window = config.RATE_LIMIT_WINDOW_SECONDS
    now = time.monotonic()
    ip = _client_ip(request)
    bucket = _hits[ip]
    while bucket and bucket[0] <= now - window:
        bucket.popleft()
    if len(bucket) >= limit:
        logger.warning("rate limit hit for ip=%s", ip)
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Try again later."},
        )
    bucket.append(now)
    return await call_next(request)


def reset_hits() -> None:
    """Clear all recorded hits (useful in tests)."""
    _hits.clear()
