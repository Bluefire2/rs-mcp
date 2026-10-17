import httpx
import pytest

from osrs_mcp.http import HttpClient

URL = "https://example.test/data"


def make_client(handler) -> HttpClient:
    return HttpClient("test-agent/1.0", backoff_base=0, transport=httpx.MockTransport(handler))


async def test_sends_user_agent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, text="ok")

    client = make_client(handler)
    await client.get(URL)
    assert seen["ua"] == "test-agent/1.0"


async def test_caches_within_ttl():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=f"call {calls}")

    client = make_client(handler)
    first = await client.get(URL, ttl=60)
    second = await client.get(URL, ttl=60)
    assert calls == 1
    assert second.text == first.text == "call 1"


async def test_no_cache_without_ttl():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="ok")

    client = make_client(handler)
    await client.get(URL)
    await client.get(URL)
    assert calls == 2


async def test_cache_key_includes_params():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=request.url.params["player"])

    client = make_client(handler)
    a = await client.get(URL, params={"player": "a"}, ttl=60)
    b = await client.get(URL, params={"player": "b"}, ttl=60)
    assert (a.text, b.text) == ("a", "b")


async def test_retries_on_5xx_then_succeeds():
    statuses = iter([503, 502, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), text="body")

    client = make_client(handler)
    response = await client.get(URL)
    assert response.status_code == 200


async def test_gives_up_after_max_retries():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    client = make_client(handler)
    response = await client.get(URL, ttl=60)
    assert response.status_code == 429
    assert calls == 4  # first try + 3 retries
    # Failures are not cached.
    await client.get(URL, ttl=60)
    assert calls == 8


async def test_does_not_retry_404_and_caches_it():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    client = make_client(handler)
    await client.get(URL, ttl=60)
    await client.get(URL, ttl=60)
    assert calls == 1


async def test_transport_error_retried_then_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = make_client(handler)
    with pytest.raises(httpx.ConnectError):
        await client.get(URL)
