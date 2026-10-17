"""Shared outbound HTTP: descriptive User-Agent, timeout, retry with backoff, and a TTL cache.

Every data-source client goes through `HttpClient` so that politeness rules (UA, caching, no
tight retry loops) live in one place.
"""

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class CachedResponse:
    status_code: int
    text: str
    fetched_at: float  # unix seconds

    def json(self) -> Any:
        return json.loads(self.text)


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._cache: dict[str, tuple[float, CachedResponse]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        ttl: float = 0,
    ) -> CachedResponse:
        """GET with caching. Only 2xx and 404 responses are cached (404 = "player not found")."""
        key = str(httpx.URL(url, params=params))
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

        response = await self._send("GET", key, headers=headers)
        cached = CachedResponse(response.status_code, response.text, time.time())
        if ttl > 0 and (response.is_success or response.status_code == 404):
            self._cache[key] = (now + ttl, cached)
        return cached

    async def post(self, url: str, *, headers: Mapping[str, str] | None = None) -> CachedResponse:
        """POST without caching or retry on 429 (callers of rate-limited writes handle it)."""
        response = await self._client.post(url, headers=headers)
        return CachedResponse(response.status_code, response.text, time.time())

    def clear_cache(self) -> None:
        self._cache.clear()

    async def _send(
        self, method: str, url: str, *, headers: Mapping[str, str] | None
    ) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, url, headers=headers)
            except httpx.TransportError:
                if attempt >= self._max_retries:
                    raise
            else:
                if response.status_code not in RETRY_STATUSES or attempt >= self._max_retries:
                    return response
            await asyncio.sleep(self._backoff_base * 2**attempt)
            attempt += 1
