"""Shared async HTTP client with per-market rate limiting, retries and an on-disk TTL cache."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx

from .config import CACHE_DIR

UA = "cs2tradeup/0.1 (+personal trade-up scanner)"


class RateLimiter:
    """Spaces requests so at most `rate` requests per second are sent."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval

    def penalize(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status   # HTTP status when the server answered, None for network errors


class Http:
    def __init__(self, rate_limits: dict[str, float], cache_dir: Path = CACHE_DIR, verbose: bool = False):
        self.client = httpx.AsyncClient(timeout=60, follow_redirects=True,
                                        headers={"User-Agent": UA, "Accept-Encoding": "br, gzip"})
        self.limiters = {k: RateLimiter(v) for k, v in rate_limits.items()}
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.verbose = verbose
        self.requests_made = 0

    async def close(self) -> None:
        await self.client.aclose()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def cache_get(self, key: str, ttl: float):
        # the fetch time is stored inside the file: copying the folder resets mtimes and would revive stale data
        p = self._cache_path(key)
        if ttl <= 0 or not p.exists():
            return None
        try:
            entry = json.loads(p.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or "fetched_at" not in entry:
            return None  # cache file from an older version
        if not 0 <= time.time() - entry["fetched_at"] < ttl:
            return None
        return entry.get("data")

    def cache_put(self, key: str, data) -> None:
        try:
            self._cache_path(key).write_text(json.dumps({"fetched_at": time.time(), "data": data}),
                                             encoding="utf8")
        except OSError:
            pass

    async def get_json(self, market: str, url: str, *, params: dict | None = None,
                       headers: dict | None = None, cache_key: str | None = None,
                       ttl: float = 0, retries: int = 4, sign=None):
        """GET a JSON document. `cache_key` must not contain secrets. `sign(url)` returns extra headers."""
        if cache_key and ttl:
            hit = self.cache_get(cache_key, ttl)
            if hit is not None:
                return hit
        limiter = self.limiters.get(market)
        if params:
            url = str(httpx.URL(url, params=params))
        delay = 2.0
        for attempt in range(retries + 1):
            if limiter:
                await limiter.wait()
            hdrs = dict(headers or {})
            if sign:
                hdrs.update(sign(url))
            try:
                r = await self.client.get(url, headers=hdrs)
            except httpx.HTTPError as e:
                if attempt == retries:
                    raise HttpError(f"{market}: {e}") from e
                await asyncio.sleep(delay)
                delay *= 2
                continue
            self.requests_made += 1
            if r.status_code == 429 or r.status_code >= 500:
                wait = _retry_after(r) or delay
                if limiter:
                    limiter.penalize(wait)
                if self.verbose:
                    print(f"[{market}] HTTP {r.status_code}, waiting {wait:.0f}s")
                if attempt == retries:
                    raise HttpError(f"{market}: HTTP {r.status_code} after {retries} retries", r.status_code)
                await asyncio.sleep(wait)
                delay *= 2
                continue
            if limiter:
                _respect_quota(r, limiter, market, self.verbose)
            if r.status_code >= 400:
                raise HttpError(f"{market}: HTTP {r.status_code}: {r.text[:200]}", r.status_code)
            data = r.json()
            if cache_key and ttl:
                self.cache_put(cache_key, data)
            return data
        raise HttpError(f"{market}: request failed")


def _respect_quota(r: httpx.Response, limiter: RateLimiter, market: str, verbose: bool) -> None:
    """If the server says the quota is (almost) used up, hold further requests until it resets."""
    rem = r.headers.get("x-ratelimit-remaining")
    if rem is None:
        return
    try:
        remaining = int(rem)
    except ValueError:
        return
    if remaining <= 1:
        wait = _retry_after(r) or 60.0
        if verbose:
            print(f"[{market}] quota exhausted, pausing {wait:.0f}s")
        limiter.penalize(wait)


def _retry_after(r: httpx.Response) -> float | None:
    for h in ("retry-after", "x-ratelimit-reset", "ratelimit-reset"):
        v = r.headers.get(h)
        if not v:
            continue
        try:
            val = float(v)
        except ValueError:
            continue
        if val > 1e9:  # epoch timestamp
            val = val - time.time()
        return max(1.0, min(val, 300.0))
    try:
        ms = r.json().get("msBeforeNext")
        if ms:
            return max(1.0, ms / 1000)
    except ValueError:
        pass
    return None
