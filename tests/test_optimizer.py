import random

import pytest

from cs2tradeup.catalog import Catalog
from cs2tradeup.config import load_config
from cs2tradeup.markets import Listing
from cs2tradeup.optimizer import Group, Problem, collection_bound, solve
from cs2tradeup.pricing import PriceBook, Quote


class FlatPrices(PriceBook):
    """Every output sells for $100 net, so the optimizer is all about input cost and floats."""

    def __init__(self):
        super().__init__(load_config())

    def quote(self, variants, wear, stattrak):
        return Quote(100.0, 100.0, 50, "test", False)


@pytest.fixture(scope="module")
def cat():
    return Catalog()


def make_listings(skin, n, rng, lo=None, hi=None, market="test"):
    lo = skin.min_float if lo is None else lo
    hi = skin.max_float if hi is None else hi
    out = []
    for i in range(n):
        fl = rng.uniform(lo, hi)
        # lower floats cost more, like real markets
        price = round(1.0 + 4.0 * (1 - (fl - skin.min_float) / skin.float_range) + rng.uniform(0, 0.5), 2)
        out.append(Listing(market, f"{skin.id}-{i}", skin, skin.name, fl, price, price, False, False,
                           f"https://example/{i}"))
    return out


def test_optimizer_hits_target_and_is_cheapest(cat):
    rng = random.Random(1)
    redline = cat.find_skin("AK-47 | Redline")
    ls = make_listings(redline, 120, rng)
    target = 0.38 - 5e-6  # FT for every output (the Phoenix Covert, AWP Asiimov, starts at 0.18)
    res = solve(cat, FlatPrices(), Problem(4, False, target, ls, objective="cost"))
    assert res is not None
    assert len(res.contract.inputs) == 10
    assert res.max_output_float <= target
    bound = collection_bound(cat, redline.collection_id, 4, False, target)
    assert res.contract.average() <= bound + 1e-9
    # brute-force sanity: no cheaper set of 10 satisfies the budget (greedy check on sorted by price)
    cheapest10 = sorted(ls, key=lambda l: l.cost)[:10]
    avg_cheap = sum((l.float_value - 0.1) / 0.6 for l in cheapest10) / 10
    if avg_cheap <= bound:
        assert res.cost == pytest.approx(sum(l.cost for l in cheapest10))


def test_optimizer_mixed_groups(cat):
    rng = random.Random(2)
    redline = cat.find_skin("AK-47 | Redline")
    other_col = next(c for c in cat.tradeup_collections(4) if c.id != redline.collection_id)
    filler = other_col.tiers[4][0]
    ls = make_listings(redline, 60, rng) + make_listings(filler, 60, rng)
    groups = [Group({redline.id}, 3, 3), Group({filler.id}, 7, 7)]
    res = solve(cat, FlatPrices(), Problem(4, False, 0.38 - 5e-6, ls, groups=groups, objective="cost"))
    assert res is not None
    names = [i.skin.name for i in res.contract.inputs]
    assert names.count(redline.name) == 3 and names.count(filler.name) == 7
    assert res.max_output_float <= 0.38


def test_optimizer_infeasible(cat):
    rng = random.Random(3)
    redline = cat.find_skin("AK-47 | Redline")
    ls = make_listings(redline, 30, rng, lo=0.5, hi=0.7)
    assert solve(cat, FlatPrices(), Problem(4, False, 0.38 - 5e-6, ls)) is None   # floats too high
    ls = make_listings(redline, 30, rng, lo=0.1, hi=0.11)
    assert solve(cat, FlatPrices(), Problem(4, False, 0.15 - 5e-6, ls)) is None   # Asiimov can't be MW
