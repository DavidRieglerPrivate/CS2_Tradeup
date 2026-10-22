"""Owned skins from the Steam inventory: parsing a real response, valuing them and using them in contracts."""
import asyncio

import httpx
import pytest

from cs2tradeup.catalog import Catalog
from cs2tradeup.config import load_config
from cs2tradeup.http import Http
from cs2tradeup.interactive import parse_numbers
from cs2tradeup.inventory import (InventoryError, fetch_pages, load_owned, owned_listings, parse_inventory,
                                  resolve_steam_id)
from cs2tradeup.markets import OWNED, Listing
from cs2tradeup.optimizer import Problem, solve
from cs2tradeup.pricing import Quote
from cs2tradeup.scanner import ListingFetcher, coarse_candidates, deep_scan, owned_first
from tests.test_fixtures import fixture
from tests.test_markets import TwelveAtFive
from tests.test_optimizer import FlatPrices
from tests.test_scan_coverage import flat_prices

SID = "76561198000000000"


@pytest.fixture(scope="module")
def cat():
    return Catalog()


def steam_http(tmp_path, handler):
    http = Http({"steam": 1000}, cache_dir=tmp_path)
    http.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return http


class PricedAt(FlatPrices):
    """Outputs sell for $100 net; every owned skin would sell for `value`."""

    def __init__(self, value):
        super().__init__()
        self.value = value

    def _quote_one(self, name, version):
        return Quote(self.value, self.value, 10, "test", False)


def test_real_inventory_response(cat):
    # captured 2026-10-21 (trimmed, certificates removed): guns, a StatTrak, a Souvenir, a knife, a case, a coin, a sticker
    items, total = parse_inventory([fixture("steam_inventory.json")], cat)
    assert total == 12
    by_name = {i.name: i for i in items}
    assert "★ Nomad Knife | Doppler (Factory New)" not in by_name   # knives can't be inputs
    assert not any("Case" in n or "Sticker" in n or "Coin" in n for n in by_name)
    tec = by_name["Tec-9 | Groundwater (Field-Tested)"]
    assert tec.skin.name == "Tec-9 | Groundwater" and tec.float_value == pytest.approx(0.3633067607)
    assert not tec.stattrak and not tec.souvenir and tec.tradable
    awp = by_name["StatTrak™ AWP | Atheris (Field-Tested)"]
    assert awp.stattrak and not awp.souvenir and awp.skin.name == "AWP | Atheris"
    mac = by_name["Souvenir MAC-10 | Gold Brick (Well-Worn)"]
    assert mac.souvenir and not mac.stattrak
    assert sum(i.skin.name == "MP7 | Army Recon" for i in items) == 2   # each copy is its own input
    assert len({i.asset_id for i in items}) == len(items)


def test_pages_continue_past_an_empty_page(tmp_path):
    # a real inventory answered its first page with no assets but more_items set
    seen = []

    def handler(req):
        seen.append(req.url.params.get("start_assetid"))
        if "start_assetid" not in req.url.params:
            return httpx.Response(200, json={"more_items": 1, "last_assetid": "5", "success": 1})
        return httpx.Response(200, json=fixture("steam_inventory.json"))

    pages = asyncio.run(fetch_pages(steam_http(tmp_path, handler), SID))
    assert len(pages) == 2 and seen == [None, "5"]


def test_private_inventory_says_how_to_fix_it(tmp_path):
    http = steam_http(tmp_path, lambda req: httpx.Response(403, text="null"))
    with pytest.raises(InventoryError, match="private"):
        asyncio.run(fetch_pages(http, SID))


@pytest.mark.parametrize("text", [SID, f"https://steamcommunity.com/profiles/{SID}/", "https://steamcommunity.com/id/someone",
                                  "someone"])
def test_steam_id_from_link_or_name(tmp_path, text):
    xml = f"<?xml version=\"1.0\"?><profile><steamID64>{SID}</steamID64></profile>"
    http = steam_http(tmp_path, lambda req: httpx.Response(200, text=xml))
    assert asyncio.run(resolve_steam_id(http, text)) == SID


def test_unknown_profile_name(tmp_path):
    http = steam_http(tmp_path, lambda req: httpx.Response(200, text="<response><error>not found</error></response>"))
    with pytest.raises(InventoryError, match="no Steam profile named"):
        asyncio.run(resolve_steam_id(http, "nobody_here"))


def test_owned_skins_cost_what_they_would_sell_for(cat):
    items, _ = parse_inventory([fixture("steam_inventory.json")], cat)
    cfg = load_config("nonexistent.toml")
    ls = owned_listings(items, PricedAt(3.0), cfg)
    assert all(l.market == OWNED and l.cost == 3.0 and not l.link for l in ls)
    cfg.raw["inventory"]["owned_cost"] = "free"
    assert all(l.cost == 0 for l in owned_listings(items, PricedAt(3.0), cfg))


def test_load_owned_keeps_only_skins_that_can_be_traded_up(cat, tmp_path):
    http = steam_http(tmp_path, lambda req: httpx.Response(200, json=fixture("steam_inventory.json")))
    cfg = load_config("nonexistent.toml")
    cfg.raw["keys"]["steam_id"] = SID
    owned, total = asyncio.run(load_owned(http, cfg, cat, PricedAt(1.0)))
    assert total == 12 and owned
    assert all(cat.collection_of(l.skin).outputs_for(l.skin.tier, l.stattrak) for l in owned)


def owned_redlines(cat, n, fl=0.16, cost=1.0):
    s = cat.find_skin("AK-47 | Redline")
    return [Listing(OWNED, f"own{i}", s, s.name, fl, cost, cost, False, False, "") for i in range(n)]


def test_solver_uses_owned_skins_and_budget_ignores_them(cat):
    s = cat.find_skin("AK-47 | Redline")
    market = [Listing("shop", f"m{i}", s, s.name, 0.20, 5.0, 5.0, False, False, "x") for i in range(10)]
    owned = owned_redlines(cat, 4)
    res = solve(cat, FlatPrices(), Problem(s.tier, False, 0.38, market + owned, objective="cost"))
    assert len(res.owned) == 4 and res.cash == pytest.approx(30.0) and res.cost == pytest.approx(34.0)
    # a $30 budget buys the 6 market skins; the 4 owned ones (worth $4) don't count against it
    res = solve(cat, FlatPrices(), Problem(s.tier, False, 0.38, market + owned, objective="cost", max_cost=30))
    assert res is not None and len(res.owned) == 4


def test_deep_scan_mixes_owned_skins_into_the_pool(cat):
    cfg = load_config("nonexistent.toml")
    redline = cat.find_skin("AK-47 | Redline")
    cands = [c for c in coarse_candidates(cat, flat_prices(), cfg, [redline.tier], [False],
                                          [("FT", 0.38 - 5e-6)], False) if c.collection.id == redline.collection_id]
    owned = owned_redlines(cat, 3, cost=0.5)
    results, cov = asyncio.run(deep_scan(cat, FlatPrices(), cfg, ListingFetcher([TwelveAtFive()], log=lambda *a: None),
                                         cands, False, log=lambda *a: None, owned=owned))
    best = max(results, key=lambda r: r.profit)
    assert len(best.owned) == 3 and best.cash == pytest.approx(35.0)


def test_ideas_the_owned_skins_fit_are_checked_first(cat):
    cfg = load_config("nonexistent.toml")
    redline = cat.find_skin("AK-47 | Redline")
    cands = coarse_candidates(cat, flat_prices(), cfg, [redline.tier], [False], [("FT", 0.38 - 5e-6)], False)
    assert len(cands) > 3
    ordered = owned_first(cands, owned_redlines(cat, 1), cfg, False)
    assert ordered[0].collection.id == redline.collection_id
    assert [c for c in ordered[1:]] == [c for c in cands if c.collection.id != redline.collection_id]
    # a StatTrak skin doesn't fit a normal contract
    st = [Listing(OWNED, "st", redline, redline.name, 0.16, 1, 1, True, False, "")]
    assert owned_first(cands, st, cfg, False) == cands


@pytest.mark.parametrize("raw, hi, expect", [("1-3, 5 7", 10, [1, 2, 3, 5, 7]), ("2,2", 5, [2]), ("0", 5, None),
                                             ("4-11", 10, None), ("a", 5, None), ("3-1", 5, None)])
def test_parse_numbers(raw, hi, expect):
    assert parse_numbers(raw, hi) == expect
