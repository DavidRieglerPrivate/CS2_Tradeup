"""Parsers run against real API responses captured on 2026-10-15 (trimmed, seller/owner ids removed).

The hand-written examples in test_markets.py follow the docs; these follow what the APIs actually send,
which is what broke DMarket and Waxpeer silently before. Refresh them with a real capture when an API changes.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from cs2tradeup.catalog import Catalog
from cs2tradeup.markets import CSFloat, DMarket, Listing, Market, Waxpeer
from cs2tradeup.optimizer import Problem, confirm_listings, solve
from cs2tradeup.pricing import PriceBook
from tests.test_markets import cfg_with_keys, make_http

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf8"))


@pytest.fixture(scope="module")
def cat():
    return Catalog()


def serve(tmp_path, routes: dict[str, object]):
    """Mock HTTP client answering by URL path; a value can be JSON data or an int status code."""
    def handler(req: httpx.Request):
        for path, body in routes.items():
            if req.url.path.endswith(path):
                return httpx.Response(body, json={}) if isinstance(body, int) else httpx.Response(200, json=body)
        raise AssertionError(f"unexpected request {req.url}")
    return make_http(tmp_path, handler)


def test_csfloat_real_response(cat, tmp_path):
    redline = cat.find_skin("AK-47 | Redline")
    http = serve(tmp_path, {"/api/v1/listings": fixture("csfloat_listings.json")})
    ls = asyncio.run(CSFloat(http, cfg_with_keys()).listings(redline, False, False, 0.25, pages=1))
    assert len(ls) == 4
    assert all(0.1 <= l.float_value <= 0.25 and l.price > 1 for l in ls)
    assert ls[0].price == 27.55 and ls[0].link.startswith("https://csfloat.com/item/")


def test_dmarket_real_response(cat, tmp_path):
    redline = cat.find_skin("AK-47 | Redline")
    http = serve(tmp_path, {"/marketplace-api/v2/offers": fixture("dmarket_offers.json")})
    cfg = cfg_with_keys()
    ls = asyncio.run(DMarket(http, cfg).listings(redline, False, False, 0.21, pages=1))
    # 4 offers: 3 tradable at or below 0.21, one with a 6-day trade lock
    assert sorted(round(l.float_value, 4) for l in ls) == [0.2054, 0.2055, 0.2077]
    cfg.raw["markets"]["include_trade_locked"] = True
    ls = asyncio.run(DMarket(http, cfg).listings(redline, False, False, 0.21, pages=1))
    assert sorted(l.trade_locked for l in ls) == [False, False, False, True]


def test_waxpeer_real_response(cat, tmp_path):
    redline = cat.find_skin("AK-47 | Redline")
    http = serve(tmp_path, {"/v1/get-items-list": fixture("waxpeer_items_list.json")})
    cfg = cfg_with_keys()
    # FT only: 0.15 <= floats; band up to 0.37 keeps 3 unlocked rows, plus one held row
    ls = asyncio.run(Waxpeer(http, cfg).listings(redline, False, False, 0.37, pages=1))
    ls = [l for l in ls if "Field-Tested" in l.name]
    assert len(ls) == 3 and not any(l.trade_locked for l in ls)
    cfg.raw["markets"]["include_trade_locked"] = True
    ls = asyncio.run(Waxpeer(http, cfg).listings(redline, False, False, 0.37, pages=1))
    ls = [l for l in ls if "Field-Tested" in l.name]
    assert len(ls) == 4 and sum(l.trade_locked for l in ls) == 1


def _listing(cat, market, lid, price, fl=0.2):
    s = cat.find_skin("AK-47 | Redline")
    return Listing(market, lid, s, s.name, fl, price, price, False, False, "")


def test_waxpeer_availability_real_response(cat, tmp_path):
    http = serve(tmp_path, {"/v1/check-availability": fixture("waxpeer_check_availability.json")})
    wx = Waxpeer(http, cfg_with_keys())
    alive = _listing(cat, "waxpeer", "53768912666", 26.826)
    sold = _listing(cat, "waxpeer", "1", 5.0)
    repriced = _listing(cat, "waxpeer", "53768912666", 20.0)
    assert asyncio.run(wx.gone([alive, sold])) == {"1"}
    assert asyncio.run(wx.gone([repriced])) == {"53768912666"}


def test_csfloat_availability_real_response(cat, tmp_path):
    single = fixture("csfloat_single.json")
    lid = single["id"]
    http = serve(tmp_path, {f"/listings/{lid}": single, "/listings/999": 400})
    cf = CSFloat(http, cfg_with_keys())
    ok = _listing(cat, "csfloat", lid, single["price"] / 100)
    assert asyncio.run(cf.gone([ok, _listing(cat, "csfloat", "999", 1.0)])) == {"999"}


def test_pricebook_loads_real_bulk_data(cat, tmp_path):
    from cs2tradeup.config import load_config

    http = serve(tmp_path, {"/v1/sales/history": fixture("skinport_history.json"),
                            "/v1/items": fixture("skinport_items.json"),
                            "/v1/prices": fixture("waxpeer_prices.json")})
    pb = PriceBook(load_config("nonexistent.toml"))
    asyncio.run(pb.load(http))
    redline = cat.find_skin("AK-47 | Redline")
    q = pb.quote([redline], "FT", False)
    assert q.price and q.net == pytest.approx(q.price * 0.98)
    # Doppler phases are separate Skinport "versions" and get combined with the phase weights
    karambit = cat.find_collection("Chroma 2").special_pool["★ Karambit | Doppler"]
    q = pb.quote(karambit, "FN", False)
    assert q.source == "skinport phase-weighted" and q.price > 1000
    glock = next(g for g in cat.find_collection("2021 Train").outputs_for(4, False) if len(g) > 1)
    assert pb.quote(glock, "FN", False).source == "skinport phase-weighted"


class Shop(Market):
    """Fake market with fixed listings, some of which have sold."""
    name = "shop"

    def __init__(self, sold):
        self.sold = set(sold)
        self.checked = []

    async def gone(self, listings):
        self.checked += [l.listing_id for l in listings]
        return {l.listing_id for l in listings if l.listing_id in self.sold}


def test_confirm_resolves_around_sold_listings(cat):
    from tests.test_optimizer import FlatPrices

    s = cat.find_skin("AK-47 | Redline")
    ls = [Listing("shop", str(i), s, s.name, 0.16 + i * 0.001, 1.0 + i * 0.1, 1.0 + i * 0.1, False, False, "")
          for i in range(14)]
    prices = FlatPrices()
    res = solve(cat, prices, Problem(s.tier, False, 0.38, ls, objective="cost"))
    assert sorted(l.listing_id for l in res.listings) == [str(i) for i in range(10)]
    shop = Shop(sold={"0", "3"})
    out, sold = asyncio.run(confirm_listings(cat, prices, [shop], [res]))
    assert sold == 2 and len(out) == 1
    ids = sorted((l.listing_id for l in out[0].listings), key=int)
    assert "0" not in ids and "3" not in ids and ids == ["1", "2", "4", "5", "6", "7", "8", "9", "10", "11"]
    # the replacement listings were checked too, each listing only once
    assert sorted(shop.checked, key=int) == [str(i) for i in range(12)]


def test_confirm_drops_result_when_nothing_is_left(cat):
    from tests.test_optimizer import FlatPrices

    s = cat.find_skin("AK-47 | Redline")
    ls = [Listing("shop", str(i), s, s.name, 0.2, 1.0, 1.0, False, False, "") for i in range(10)]
    res = solve(cat, FlatPrices(), Problem(s.tier, False, 0.38, ls, objective="cost"))
    out, sold = asyncio.run(confirm_listings(cat, FlatPrices(), [Shop(sold={"4"})], [res]))
    assert out == [] and sold == 1


def test_doctor_flags_a_market_that_returns_nothing(cat, monkeypatch):
    from cs2tradeup import actions, markets
    from cs2tradeup.config import load_config

    async def fake_load(self, http):
        for r in fixture("skinport_history.json") * 100:
            self.sales[(r["market_hash_name"] + str(len(self.sales)), r.get("version"))] = r
        for r in fixture("skinport_history.json"):
            self.sales[(r["market_hash_name"], r.get("version"))] = r
        for r in fixture("skinport_items.json"):
            self.skinport[(r["market_hash_name"], r.get("version"))] = r
        self.waxpeer = {str(i): {} for i in range(2000)}

    class Empty(Market):
        name = "empty"
        available = True

        async def listings(self, skin, stattrak, souvenir, max_float):
            return []

    class Good(Empty):
        name = "good"

        async def listings(self, skin, stattrak, souvenir, max_float):
            return [Listing("good", "1", skin, skin.name, 0.2, 30.0, 30.0, False, False, "")]

    monkeypatch.setattr(PriceBook, "load", fake_load)
    monkeypatch.setattr(markets, "MARKETS", {"empty": Empty, "good": Good})
    cfg = load_config("nonexistent.toml")
    cfg.raw["markets"]["enabled"] = ["empty", "good", "nope"]
    checks = {c.name: c for c in actions.run_doctor(cfg, cat)}
    assert checks["Skinport prices"].status == "ok"
    assert checks["empty"].status == "fail" and "0 listings" in checks["empty"].detail
    assert checks["good"].status == "ok"
    assert checks["nope"].status == "fail"
