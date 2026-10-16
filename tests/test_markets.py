"""Market adapters parsed against canned API responses (shapes taken from each API's docs)."""
import asyncio
import json
import random
import time

import httpx
import pytest

from cs2tradeup.catalog import Catalog
from cs2tradeup.config import load_config
from cs2tradeup.http import Http, HttpError
from cs2tradeup.markets import CSFloat, DMarket, Listing, Market, Waxpeer
from cs2tradeup.scanner import ListingFetcher, coarse_candidates, deep_scan
from tests.test_optimizer import FlatPrices


@pytest.fixture(scope="module")
def cat():
    return Catalog()


def make_http(tmp_path, handler):
    http = Http({"csfloat": 1000, "dmarket": 1000, "waxpeer": 1000}, cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return http


def cfg_with_keys():
    cfg = load_config("nonexistent.toml")
    cfg.raw["keys"] = {"csfloat": "cf-key", "waxpeer": "wx-key",
                       "dmarket_public": "ab" * 32, "dmarket_secret": "cd" * 64}
    return cfg


def test_csfloat_parsing(cat, tmp_path):
    redline = cat.find_skin("AK-47 | Redline")
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers.get("Authorization")
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"data": [
            {"id": "111", "price": 1234, "type": "buy_now",
             "item": {"float_value": 0.151, "market_hash_name": "AK-47 | Redline (Field-Tested)"}},
            {"id": "112", "price": 999, "type": "auction", "item": {"float_value": 0.16}},
        ], "cursor": None})

    http = make_http(tmp_path, handler)
    ls = asyncio.run(CSFloat(http, cfg_with_keys()).listings(redline, False, False, 0.2))
    assert seen["auth"] == "cf-key"
    assert seen["params"]["def_index"] == "7" and seen["params"]["paint_index"] == "282"
    assert seen["params"]["category"] == "1" and seen["params"]["max_float"] == "0.2"
    assert len(ls) == 1 and ls[0].price == 12.34 and ls[0].link == "https://csfloat.com/item/111"


def test_dmarket_parsing_and_signature(cat, tmp_path):
    redline = cat.find_skin("AK-47 | Redline")
    seen = {}

    def handler(req: httpx.Request):
        seen["headers"] = dict(req.headers)
        seen["params"] = dict(req.url.params)
        item = lambda pi, fl, price: {"offerId": f"o{pi}{fl}", "priceCents": str(price),
                                      "attributes": {"title": "AK-47 | Redline (Field-Tested)", "tradable": True,
                                                     "tradeLockDays": 0,
                                                     "cs2": {"float": str(fl), "paintIndex": pi, "defIndex": 7}}}
        return httpx.Response(200, json={"items": [item(282, 0.17, 1599), item(999, 0.17, 100),
                                                    item(282, 0.5, 900)]})

    http = make_http(tmp_path, handler)
    ls = asyncio.run(DMarket(http, cfg_with_keys()).listings(redline, True, False, 0.3))
    assert seen["headers"]["x-request-sign"].startswith("dmar ed25519 ")
    assert len(seen["headers"]["x-request-sign"]) == len("dmar ed25519 ") + 128
    assert seen["params"]["title"] == "StatTrak™ AK-47 | Redline"
    filters = seen["params"]["treeFilters"].split(",")
    assert "category=stattrak_tm" in filters
    # only float buckets that can hold a Redline at or below 0.3 (Redline starts at 0.10)
    assert [f for f in filters if f.startswith("floatPart")] == [
        "floatPart[]=MW-3", "floatPart[]=MW-4", "floatPart[]=FT-0", "floatPart[]=FT-1",
        "floatPart[]=FT-2", "floatPart[]=FT-3", "floatPart[]=FT-4"]
    assert [l.price for l in ls] == [15.99]       # wrong paint index and too-high float dropped


def test_dmarket_float_parts_cover_every_float():
    from cs2tradeup.markets import DMARKET_FLOAT_PARTS, dmarket_float_parts

    for a, b in zip(DMARKET_FLOAT_PARTS, DMARKET_FLOAT_PARTS[1:]):
        assert a[2] == b[1]                       # contiguous, no gaps
    assert DMARKET_FLOAT_PARTS[0][1] == 0.0 and DMARKET_FLOAT_PARTS[-1][2] == 1.0
    assert dmarket_float_parts(0.0, 0.069) == [f"FN-{i}" for i in range(7)]
    assert dmarket_float_parts(0.15, 0.2) == ["FT-0", "FT-1"]


def test_waxpeer_parsing(cat, tmp_path):
    redline = cat.find_skin("AK-47 | Redline")
    calls = []

    def handler(req: httpx.Request):
        name, skip = req.url.params["search"], int(req.url.params["skip"])
        calls.append((name, skip))
        assert req.url.params["order_by"] == "price"
        if name.endswith("(Field-Tested)") and skip == 0:   # a full page with nothing in the band
            rows = [{"name": name, "price": 9000 + i, "item_id": f"h{i}", "float": 0.29} for i in range(100)]
        else:
            rows = [
                {"name": name, "price": 15500, "item_id": f"9-{skip}", "float": 0.14},
                {"name": name, "price": 9000, "item_id": f"10-{skip}", "float": 0.29},
                {"name": "StatTrak™ " + name, "price": 100, "item_id": f"11-{skip}", "float": 0.12},
            ]
        return httpx.Response(200, json={"success": True, "items": rows})

    http = make_http(tmp_path, handler)
    ls = asyncio.run(Waxpeer(http, cfg_with_keys()).listings(redline, False, False, 0.2))
    # MW fits on the first page (short page = last page); FT's first page had nothing usable, so page 2 is read
    assert calls == [("AK-47 | Redline (Minimal Wear)", 0), ("AK-47 | Redline (Field-Tested)", 0),
                     ("AK-47 | Redline (Field-Tested)", 100)]
    # StatTrak rows returned by the substring search and too-high floats are dropped
    assert sorted(l.listing_id for l in ls) == ["9-0", "9-100"]
    assert {l.price for l in ls} == {15.5}


def test_fetcher_skips_souvenirs_nobody_sells(cat):
    redline = cat.find_skin("AK-47 | Redline")
    asked = []

    class Recorder(FakeMarket):
        async def listings(self, skin, stattrak, souvenir, max_float):
            asked.append(souvenir)
            return []

    class Prices:
        def listed_anywhere(self, skin, stattrak, souvenir):
            return False

    fetcher = ListingFetcher([Recorder()], log=lambda *a: None, prices=Prices())
    asyncio.run(fetcher.get(redline, False, True, 0.3))
    asyncio.run(fetcher.get(redline, False, False, 0.3))
    assert asked == [False] and fetcher.skipped == 1


class FakeMarket(Market):
    """Generates a realistic spread of listings for any skin."""
    name = "fake"

    def __init__(self):
        self.buy_fee = 0.0
        self.include_locked = False
        self.rng = random.Random(7)

    async def listings(self, skin, stattrak, souvenir, max_float):
        out = []
        for i in range(40):
            fl = self.rng.uniform(skin.min_float, max_float)
            norm = (fl - skin.min_float) / skin.float_range
            price = round(0.5 + 2.0 * (1 - norm) + self.rng.uniform(0, 0.3), 2)
            out.append(Listing("fake", f"{skin.id}-{souvenir}-{i}", skin, skin.name, fl, price, price,
                               stattrak, souvenir, f"https://fake/{skin.id}/{i}"))
        return out


def test_deep_scan_end_to_end(cat):
    cfg = load_config("nonexistent.toml")
    prices = FlatPrices()
    prices.cheapest = lambda skin, wear, st, souv: type("O", (), {"price": 1.0, "market": "fake", "quantity": 5,
                                                                  "page": ""})()
    cands = coarse_candidates(cat, prices, cfg, [3], [False], [("FT", 0.38 - 5e-6)], False)
    assert cands
    fetcher = ListingFetcher([FakeMarket()], log=lambda *a: None)
    results, cov = asyncio.run(deep_scan(cat, prices, cfg, fetcher, cands[:3], False, log=lambda *a: None))
    assert results
    assert cov.checked == cov.ideas == 3 and not cov.timed_out
    for r in results:
        assert len(r.contract.inputs) == 10
        assert r.max_output_float <= 0.38
        assert all(i.link.startswith("https://fake/") for i in r.contract.inputs)
    # sorted best-first; with flat output prices the free mix collapses onto a pure contract and is deduped
    assert [r.profit for r in results] == sorted((r.profit for r in results), reverse=True)
    assert not any(r.label.startswith("Mixed best (1 ") for r in results)


class SlowFake(FakeMarket):
    """FakeMarket behind a rate limit: one answer every `interval` seconds."""

    def __init__(self, interval):
        super().__init__()
        self.interval = interval
        self.lock = asyncio.Lock()

    async def listings(self, skin, stattrak, souvenir, max_float):
        async with self.lock:
            await asyncio.sleep(self.interval)
        return await super().listings(skin, stattrak, souvenir, max_float)


def test_deep_scan_stops_at_deadline_and_checks_best_ideas_first(cat):
    cfg = load_config("nonexistent.toml")
    prices = FlatPrices()
    prices.cheapest = lambda skin, wear, st, souv: type("O", (), {"price": 1.0, "market": "fake", "quantity": 5,
                                                                  "page": ""})()
    cands = coarse_candidates(cat, prices, cfg, [3], [False], [("FT", 0.38 - 5e-6)], False)[:8]
    fetcher = ListingFetcher([SlowFake(0.03)], log=lambda *a: None)
    skins = sum(len(c.collection.tiers[3]) for c in cands)
    deadline = time.time() + 0.03 * skins / 2     # time for about half the skins
    results, cov = asyncio.run(deep_scan(cat, prices, cfg, fetcher, cands, False, log=lambda *a: None,
                                         deadline=deadline))
    assert cov.timed_out and 0 < cov.checked < cov.ideas
    # requests are served in the order the ideas were ranked, so the ones checked are the top of the list
    done = {c.collection.name for c in cands[:cov.checked]}
    assert results and all(r.label.split(" | ")[0] in done for r in results if not r.label.startswith("Mixed"))


class FakeWaxpeer(Market):
    name = "waxpeer"
    buy_fee = 0.0

    def __init__(self):
        self.calls = []

    async def listings(self, skin, stattrak, souvenir, max_float, wears=None):
        self.calls.append(tuple(wears))
        return []


class TwelveAtFive(FakeMarket):
    """12 Field-Tested listings at $5."""

    async def listings(self, skin, stattrak, souvenir, max_float):
        return [Listing("fake", f"f{i}", skin, skin.name, 0.20, 5.0, 5.0, stattrak, souvenir, "") for i in range(12)]


@pytest.mark.parametrize("ft_min, searched", [(6000, [("MW",)]), (4000, [("MW",), ("FT",)])])
def test_waxpeer_searched_only_where_it_can_beat_the_other_markets(cat, ft_min, searched):
    redline = cat.find_skin("AK-47 | Redline")   # 0.10-0.70: MW, FT, WW, BS fit under 0.70
    prices = type("P", (), {"listed_anywhere": lambda *a: True})()
    prices.waxpeer = {
        "AK-47 | Redline (Minimal Wear)": {"min": 9000, "count": 3},     # nothing else found in MW: searched
        "AK-47 | Redline (Field-Tested)": {"min": ft_min, "count": 50},  # vs 10th cheapest FT elsewhere ($5)
        "AK-47 | Redline (Well-Worn)": {"min": 1000, "count": 0},        # nothing listed: skipped
    }                                                                    # BS missing entirely: skipped
    wax = FakeWaxpeer()
    fetcher = ListingFetcher([TwelveAtFive(), wax], log=lambda *a: None, prices=prices)
    asyncio.run(fetcher.get(redline, False, False, 0.70, need=10))
    assert wax.calls == searched
    cov = fetcher.coverage
    assert cov.slow_searched == len(searched) and cov.slow_skipped_none == 2
    assert cov.slow_skipped_price == (1 if ft_min == 6000 else 0)


class LimitedFake(FakeMarket):
    """Answers with a used-up request limit until `reset`."""
    name = "limited"

    def __init__(self, reset_in):
        super().__init__()
        self.reset = time.time() + reset_in

    def resume_at(self):
        return self.reset if time.time() < self.reset else None

    async def listings(self, skin, stattrak, souvenir, max_float):
        if time.time() < self.reset:
            raise HttpError("limited: request limit used up", 429)
        return await super().listings(skin, stattrak, souvenir, max_float)


def test_skins_skipped_for_a_used_up_limit_are_asked_again_after_the_reset(cat):
    redline = cat.find_skin("AK-47 | Redline")
    fetcher = ListingFetcher([LimitedFake(0.2)], log=lambda *a: None)
    assert asyncio.run(fetcher.get(redline, False, False, 0.3)) == []
    assert fetcher.limited and not fetcher.errors        # remembered, not reported as a failure
    asyncio.run(fetcher.retry_limited(time.time() + 5))
    assert not fetcher.limited and len(fetcher.cache[(redline.id, False, False)][1]) == 40


def test_deep_scan_fills_in_a_limited_market_after_its_reset(cat):
    cfg = load_config("nonexistent.toml")
    prices = FlatPrices()
    prices.cheapest = lambda skin, wear, st, souv: type("O", (), {"price": 1.0, "market": "fake", "quantity": 5,
                                                                  "page": ""})()
    cands = coarse_candidates(cat, prices, cfg, [3], [False], [("FT", 0.38 - 5e-6)], False)[:2]
    fetcher = ListingFetcher([LimitedFake(0.3)], log=lambda *a: None)
    results, cov = asyncio.run(deep_scan(cat, prices, cfg, fetcher, cands, False, log=lambda *a: None,
                                         deadline=time.time() + 5))
    assert results and not cov.limited and not cov.timed_out


def test_no_retry_when_the_limit_resets_after_the_deadline(cat):
    redline = cat.find_skin("AK-47 | Redline")
    fetcher = ListingFetcher([LimitedFake(60)], log=lambda *a: None)
    asyncio.run(fetcher.get(redline, False, False, 0.3))
    started = time.time()
    asyncio.run(fetcher.retry_limited(time.time() + 1))
    assert time.time() - started < 0.5 and fetcher.limited["limited"]


def test_csfloat_keeps_a_reserve_for_rechecks(cat, tmp_path):
    def handler(req):
        raise AssertionError("no request should be sent once only the reserve is left")

    http = make_http(tmp_path, handler)
    http.quota["csfloat"] = {"remaining": 30, "limit": 200, "reset_at": time.time() + 600}
    m = CSFloat(http, cfg_with_keys())
    with pytest.raises(HttpError) as e:
        asyncio.run(m.listings(cat.find_skin("AK-47 | Redline"), False, False, 0.3))
    assert e.value.status == 429 and m.resume_at() == http.quota["csfloat"]["reset_at"]
