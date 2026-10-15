from cs2tradeup.config import load_config
from cs2tradeup.pricing import PriceBook


def book():
    pb = PriceBook(load_config("nonexistent.toml"))
    name = "StatTrak™ AK-47 | Red Laminate (Field-Tested)"
    # real numbers seen on 2026-10-15: 2 outlier sales in 7 days, normal market around $130-140
    pb.sales[(name, None)] = {
        "last_7_days": {"median": 685.46, "volume": 2},
        "last_30_days": {"median": 139.94, "volume": 6},
        "last_90_days": {"median": 136.28, "volume": 13},
    }
    pb.waxpeer[name] = {"min": 130438, "count": 24}
    return pb, name


def test_thin_window_skipped_and_capped():
    pb, name = book()
    q = pb._quote_one(name, None)
    assert q.price == 130.438                     # 30-day median 139.94, capped at the cheapest listing
    assert "capped" in q.source and not q.illiquid


def test_no_cap_uses_first_window_with_enough_sales():
    pb, name = book()
    pb.cfg["pricing"]["cap_at_lowest_listing"] = False
    q = pb._quote_one(name, None)
    assert q.price == 139.94 and q.volume == 6


def test_no_sales_ignores_lone_asking_price():
    pb = PriceBook(load_config("nonexistent.toml"))
    name = "★ Bowie Knife | Crimson Web (Factory New)"
    pb.waxpeer[name] = {"min": 46478150, "count": 1}       # seen 2026-10-15: one $46k asking price
    q = pb._quote_one(name, None)
    assert q.price is None and q.illiquid
    assert pb.needs_reference(name, None)
    pb.reference[name] = 2100.0                            # CSFloat reference price
    q = pb._quote_one(name, None)
    assert q.price == 2100.0 and q.source == "csfloat reference price"


def test_all_windows_thin_is_flagged():
    pb, name = book()
    for w in pb.sales[(name, None)].values():
        w["volume"] = 1
    pb.waxpeer.clear()
    q = pb._quote_one(name, None)
    assert q.illiquid and q.price == 136.28       # longest window


def test_config_changes_do_not_leak_into_defaults(tmp_path):
    from cs2tradeup.config import DEFAULTS, load_config

    p = tmp_path / "c.toml"
    p.write_text('[keys]\ncsfloat = ""\n', encoding="utf8")
    load_config(p)["scan"]["max_contract_cost"] = 50
    load_config(tmp_path / "missing.toml")["scan"]["max_contract_cost"] = 70
    assert load_config(p)["scan"]["max_contract_cost"] == 0
    assert DEFAULTS["scan"]["max_contract_cost"] == 0


def test_cache_age_comes_from_the_file_not_its_mtime(tmp_path):
    import json
    import time

    from cs2tradeup.http import Http

    http = Http({}, cache_dir=tmp_path)
    http.cache_put("k", {"a": 1})
    assert http.cache_get("k", 60) == {"a": 1}
    # a copied cache file gets a fresh mtime, but its stored fetch time is still old
    path = http._cache_path("k")
    path.write_text(json.dumps({"fetched_at": time.time() - 3600, "data": {"a": 1}}), encoding="utf8")
    assert http.cache_get("k", 60) is None
    path.write_text(json.dumps({"a": 1}), encoding="utf8")    # old format without a fetch time
    assert http.cache_get("k", 60) is None


def test_risk_numbers():
    from types import SimpleNamespace as NS

    from cs2tradeup.evaluate import Result
    from cs2tradeup.pricing import Quote

    def outcome(p, net):
        return NS(outcome=NS(probability=p), quote=Quote(net, net, 10, "t", False))

    r = Result(None, [outcome(0.5, 100.0), outcome(0.5, 0.0)], cost=40.0, ev=50.0, missing_prices=0)
    assert r.spread == 50.0
    assert r.big_loss_chance == 0.5
    assert r.runs_needed == 68            # ceil((1.645 * 50 / 10) ** 2)
    assert Result(None, [outcome(1.0, 30.0)], cost=40.0, ev=30.0, missing_prices=0).runs_needed is None


def test_used_up_quota_skips_the_market_instead_of_stalling(tmp_path):
    import asyncio
    import time

    import httpx
    import pytest

    from cs2tradeup.http import Http, HttpError

    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/limited":   # CSFloat-style: window resets in 20 minutes
            return httpx.Response(429, headers={"x-ratelimit-limit": "200", "x-ratelimit-remaining": "0",
                                                "x-ratelimit-reset": str(int(time.time()) + 1200)})
        return httpx.Response(200, json={"ok": True}, headers={"x-ratelimit-limit": "200",
                                                                "x-ratelimit-remaining": "1",
                                                                "x-ratelimit-reset": str(int(time.time()) + 900)})

    async def run():
        h = Http({"cf": 100.0, "other": 100.0}, cache_dir=tmp_path)
        h.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        t0 = time.monotonic()
        with pytest.raises(HttpError) as e:
            await h.get_json("cf", "https://x/limited")
        assert e.value.status == 429 and "resets at" in str(e.value)
        with pytest.raises(HttpError):              # no request sent while blocked
            await h.get_json("cf", "https://x/ok")
        assert await h.get_json("other", "https://x/ok") == {"ok": True}   # other markets keep going
        assert h.quota["other"]["remaining"] == 1 and h.quota["other"]["limit"] == 200
        with pytest.raises(HttpError):              # 1 left with a 15-minute reset: blocked after this one
            await h.get_json("other", "https://x/ok")
        assert time.monotonic() - t0 < 5
        await h.client.aclose()

    asyncio.run(run())
    assert calls == ["/limited", "/ok"]
