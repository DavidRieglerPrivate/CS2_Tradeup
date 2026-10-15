"""Market adapters parsed against canned API responses (shapes taken from each API's docs)."""
import asyncio
import json
import random

import httpx
import pytest

from cs2tradeup.catalog import Catalog
from cs2tradeup.config import load_config
from cs2tradeup.http import Http
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
    results = asyncio.run(deep_scan(cat, prices, cfg, fetcher, cands[:3], False, log=lambda *a: None))
    assert results
    for r in results:
        assert len(r.contract.inputs) == 10
        assert r.max_output_float <= 0.38
        assert all(i.link.startswith("https://fake/") for i in r.contract.inputs)
    # sorted best-first; with flat output prices the free mix collapses onto a pure contract and is deduped
    assert [r.profit for r in results] == sorted((r.profit for r in results), reverse=True)
