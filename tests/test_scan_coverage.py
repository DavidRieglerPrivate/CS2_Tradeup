"""Full-coverage scans: deadlines, used-up request limits, progress text and the menu's time choices."""
import asyncio
import os
import time

import pytest

from cs2tradeup import interactive
from cs2tradeup.actions import ScanOutcome, limited_note
from cs2tradeup.catalog import Catalog
from cs2tradeup.config import load_config
from cs2tradeup.display import coverage_lines, progress_text
from cs2tradeup.http import Http, HttpError
from cs2tradeup.scanner import Coverage, ListingFetcher, coarse_candidates, deep_scan
from tests.test_markets import FakeMarket, FakeWaxpeer, LimitedFake
from tests.test_optimizer import FlatPrices


@pytest.fixture(scope="module")
def cat():
    return Catalog()


def flat_prices():
    prices = FlatPrices()
    prices.cheapest = lambda skin, wear, st, souv: type("O", (), {"price": 1.0, "market": "fake", "quantity": 5,
                                                                  "page": ""})()
    return prices


def candidates(cat, n):
    cfg = load_config("nonexistent.toml")
    return cfg, coarse_candidates(cat, flat_prices(), cfg, [3], [False], [("FT", 0.38 - 5e-6)], False)[:n]


class SlowAfterReset(LimitedFake):
    """Limited until `reset`, then each answer takes `delay` seconds."""

    def __init__(self, reset_in, delay):
        super().__init__(reset_in)
        self.delay = delay

    async def listings(self, skin, stattrak, souvenir, max_float):
        out = await super().listings(skin, stattrak, souvenir, max_float)
        await asyncio.sleep(self.delay)
        return out


def test_retry_cut_off_by_the_deadline_keeps_the_unanswered_skins(cat):
    cfg, cands = candidates(cat, 3)
    fetcher = ListingFetcher([SlowAfterReset(0.1, 0.3)], log=lambda *a: None)
    results, cov = asyncio.run(deep_scan(cat, flat_prices(), cfg, fetcher, cands, False, log=lambda *a: None,
                                         deadline=time.time() + 2.0))   # retry starts ~1.1 s in (1 s margin)
    skins = cov.skins
    # every skin was limited at first; the retry answered one or two before the deadline, the rest stay queued
    assert 0 < cov.limited["limited"] < skins
    assert sum(len(v) for v in fetcher.limited.values()) == cov.limited["limited"]


def test_a_bug_in_a_fetch_is_reported_not_swallowed(cat):
    class Broken(FakeMarket):
        async def listings(self, skin, stattrak, souvenir, max_float):
            return None   # not a list: extending with it raises inside the fetch

    cfg, cands = candidates(cat, 1)
    fetcher = ListingFetcher([Broken()], log=lambda *a: None)
    results, cov = asyncio.run(deep_scan(cat, flat_prices(), cfg, fetcher, cands, False, log=lambda *a: None))
    assert results == [] and any(e.startswith("internal error") for e in fetcher.errors)


def test_waxpeer_limit_skips_the_skin_without_counting_it_as_out_of_time(cat):
    class LimitedWaxpeer(FakeWaxpeer):
        def resume_at(self):
            return time.time() + 600

        async def listings(self, skin, stattrak, souvenir, max_float, wears=None):
            self.calls.append(tuple(wears))
            raise HttpError("waxpeer: request limit used up", 429)

    redline = cat.find_skin("AK-47 | Redline")
    prices = type("P", (), {"listed_anywhere": lambda *a: True})()
    prices.waxpeer = {f"AK-47 | Redline ({w})": {"min": 1000, "count": 5}
                      for w in ("Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred")}
    wax = LimitedWaxpeer()
    fetcher = ListingFetcher([wax], log=lambda *a: None, prices=prices)
    asyncio.run(fetcher.get(redline, False, False, 0.70))
    assert len(wax.calls) == 1                       # gave up on the skin's other wears
    assert fetcher.limited_counts() == {"waxpeer": 1} and not fetcher.errors
    assert fetcher.slow_wanted - fetcher.coverage.slow_searched == 0


def test_old_cache_files_are_pruned(tmp_path):
    http = Http({}, cache_dir=tmp_path)
    http.cache_put("old", {"a": 1})
    http.cache_put("new", {"b": 2})
    old = http._cache_path("old")
    os.utime(old, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    assert http.prune_cache() == 1
    assert not old.exists() and http.cache_get("new", 60) == {"b": 2}
    asyncio.run(http.close())


def test_limited_note_names_the_market_and_reset_time(cat):
    redline = cat.find_skin("AK-47 | Redline")
    m = LimitedFake(600)
    fetcher = ListingFetcher([m], log=lambda *a: None)
    asyncio.run(fetcher.get(redline, False, False, 0.3))
    note = limited_note(fetcher, [m])
    assert "limited's request limit is used up, it resets at" in note and "1 skin(s)" in note


@pytest.mark.parametrize("cov, deadline, expect", [
    (Coverage(ideas=10, checked=4, skins=30, skins_done=12), time.time() + 65, "Checked 4 of 10 ideas"),
    (Coverage(ideas=10, checked=10, slow_searched=5, slow_unreached=7, seconds=61), None, "1:01 so far"),
    (Coverage(ideas=10, waiting=("csfloat", time.time() + 600)), None, "csfloat's request limit to reset"),
    (Coverage(ideas=10, solving=8), None, "Picking the best skins for 8"),
])
def test_progress_text(cov, deadline, expect):
    assert expect in progress_text(cov, deadline)


def test_coverage_lines_explain_what_was_left_out():
    text = "\n".join(coverage_lines(Coverage(ideas=10, checked=6, slow_searched=3, slow_skipped_none=2,
                                             slow_skipped_price=1, slow_unreached=4, limited={"csfloat": 9},
                                             seconds=125)))
    assert "6 of 10 ideas in 2:05" in text and "other 4 ideas" in text
    assert "4 not reached in time" in text and "9 skin(s) were checked without csfloat" in text
    complete = coverage_lines(Coverage(ideas=5, checked=5))
    assert len(complete) == 1


@pytest.mark.parametrize("depth, minutes", [(1, 2), (2, 5), (3, 15), (4, None)])
def test_menu_depth_is_a_time_limit_over_all_ideas(cat, monkeypatch, depth, minutes):
    calls = {}
    answers = iter([6, 2, depth])   # rarity, StatTrak mode, depth: the condition question is stubbed below
    monkeypatch.setattr(interactive, "choose", lambda *a, **kw: next(answers))
    monkeypatch.setattr(interactive, "ask_condition", lambda allow_any: [("FT", 0.38)])
    monkeypatch.setattr(interactive, "ask_money", lambda q: None)
    monkeypatch.setattr(interactive.Confirm, "ask", lambda *a, **kw: True)
    monkeypatch.setattr(interactive, "markets_status", lambda cfg: {"CSFloat": True})

    def fake_scan(*args, **kw):
        calls.update(kw)
        return ScanOutcome(candidates=[1], deep_ran=True, coverage=Coverage(ideas=1, checked=1))

    monkeypatch.setattr(interactive, "run_scan", fake_scan)
    interactive.action_scan(cat)
    assert calls["time_limit"] == minutes and calls["top"] is None
