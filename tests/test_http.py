"""Rate-limit handling against a server that keeps refusing."""
import asyncio
import time

import httpx
import pytest

from cs2tradeup import http as http_mod
from cs2tradeup.http import Http, HttpError


def test_market_refusing_despite_a_past_reset_time_gets_blocked_not_hammered(tmp_path, monkeypatch):
    # CSFloat seen on 2026-10-16: 429 with x-ratelimit-reset already in the past, again and again
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    sent = []

    def handler(req):
        sent.append(req.url)
        return httpx.Response(429, headers={"x-ratelimit-remaining": "0",
                                            "x-ratelimit-reset": str(int(time.time()) - 30)},
                              json={"message": "too many requests"})

    http = Http({"cf": 1000.0}, cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def many():
        # 50 requests queued at once, like a scan does
        return await asyncio.gather(*(http.get_json("cf", f"https://x/{i}") for i in range(50)),
                                    return_exceptions=True)

    out = asyncio.run(many())
    assert all(isinstance(e, HttpError) and e.status == 429 for e in out)
    assert len(sent) <= http_mod.STRIKES + 1          # blocked after a few, the queued rest never sent
    assert http.blocked_until["cf"] - time.time() > http_mod.MAX_WAIT


def test_a_success_clears_the_strikes(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    answers = iter([429, 429, 200, 429, 429, 200])

    def handler(req):
        return httpx.Response(next(answers), json={})

    http = Http({}, cache_dir=tmp_path)   # no rate limiter: its back-off windows would pass in real time
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(http.get_json("cf", "https://x/1"))
    asyncio.run(http.get_json("cf", "https://x/2"))
    assert http.strikes["cf"] == 0 and "cf" not in http.blocked_until
    assert http.throttled["cf"] == 4 and http.sent["cf"] == 6


def test_limiter_serves_the_most_urgent_first_at_the_set_rate():
    from cs2tradeup.http import PRIORITY, RateLimiter

    order = []

    async def run():
        lim = RateLimiter(50.0)   # one every 20 ms

        async def req(prio, name):
            PRIORITY.set(prio)
            await lim.wait()
            order.append(name)

        t0 = time.monotonic()
        doomed = asyncio.create_task(req(0, "cancelled"))
        tasks = [asyncio.create_task(req(p, n)) for p, n in [(5, "late"), (1, "first"), (3, "second")]]
        await asyncio.sleep(0)
        doomed.cancel()
        await asyncio.gather(*tasks)
        return time.monotonic() - t0

    took = asyncio.run(run())
    assert order == ["first", "second", "late"]
    assert took >= 0.035   # three grants need two full intervals


async def _no_sleep(*a, **kw):
    await _real_sleep(0)


_real_sleep = asyncio.sleep
