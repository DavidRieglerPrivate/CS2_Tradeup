"""Pick the actual listings for a contract with CP-SAT.

Every listing is a yes/no variable. Constraints:
  * exactly N inputs, optional min/max count per input group (fixed or mixed compositions)
  * sum of normalized floats <= N * (allowed average) for every collection that is used,
    so every possible outcome lands at or below the target float (float caps included)
Objective: lowest cost, or highest expected profit when the composition is free.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .catalog import Catalog
from .engine import Contract, ContractInput, contract_size, max_avg_for, normalize
from .evaluate import Result, evaluate
from .markets import OWNED, Listing
from .pricing import PriceBook
from .wear import WEAR_BY_CODE, possible_wears

SCALE = 1_000_000


@dataclass
class Group:
    """A set of allowed input skins with a count range, e.g. 3x any Redline, or 7x from a collection."""
    skin_ids: set[str]
    min_count: int = 0
    max_count: int = 10
    label: str = ""


@dataclass
class Problem:
    tier: int
    stattrak: bool
    max_output_float: float
    listings: list[Listing]
    groups: list[Group] = field(default_factory=list)
    objective: str = "profit"         # "profit" or "cost"
    max_cost: float = 0.0
    formula: str = "normalized"


def collection_bound(catalog: Catalog, collection_id: str, tier: int, stattrak: bool, target: float) -> float:
    """Largest average that keeps every outcome of this collection <= target (negative = impossible)."""
    pool = catalog.collections[collection_id].outputs_for(tier, stattrak)
    return min((max_avg_for(g[0], target) for g in pool), default=-1.0)


def collection_value(catalog: Catalog, prices: PriceBook, collection_id: str, tier: int, stattrak: bool,
                     target: float) -> tuple[float, int]:
    """Expected net value contributed per input slot from this collection, guaranteed for any float <= target.

    Each outcome is valued at its cheapest wear among those it can land in (its min float up to the target).
    A worse wear is usually cheaper, but not always: a rare MW M4A1-S | Fade sells above FN.
    Returns (sum over outcomes of net/len(pool), number of outcomes without a price).
    Multiply by 1/N for the per-input contribution.
    """
    pool = catalog.collections[collection_id].outputs_for(tier, stattrak)
    total, missing = 0.0, 0
    for g in pool:
        s = g[0]
        if s.is_vanilla:
            nets = [prices.quote(g, None, stattrak).net]
        else:
            top = min(target, s.max_float)
            nets = [prices.quote(g, w, stattrak).net for w in possible_wears(s.min_float, s.max_float)
                    if WEAR_BY_CODE[w][2] <= top]
        nets = [n for n in nets if n is not None]
        if not nets:
            missing += 1
        else:
            total += min(nets)
    return (total / len(pool) if pool else 0.0), missing


def _prune(listings: list[Listing], key, n: int) -> list[Listing]:
    """Drop listings dominated (cheaper AND lower float) by at least n others of the same class."""
    by = defaultdict(list)
    for l in listings:
        by[key(l)].append(l)
    out = []
    for group in by.values():
        group.sort(key=lambda l: (l.cost, l.float_value))
        kept: list[Listing] = []
        for l in group:
            dominated = sum(1 for k in kept if k.cost <= l.cost and k._norm <= l._norm)
            if dominated < n:
                kept.append(l)
        out.extend(kept)
    return out


def solve(catalog: Catalog, prices: PriceBook, prob: Problem, time_limit: float = 20.0) -> Result | None:
    n = contract_size(prob.tier)
    target = prob.max_output_float

    # attach normalized values; drop listings whose collection can't reach the target at all
    bounds: dict[str, float] = {}
    values: dict[str, float] = {}
    cands: list[Listing] = []
    seen = set()
    for l in prob.listings:
        if (l.market, l.listing_id) in seen:
            continue
        seen.add((l.market, l.listing_id))
        cid = l.skin.collection_id
        if cid not in bounds:
            bounds[cid] = collection_bound(catalog, cid, prob.tier, prob.stattrak, target)
            values[cid] = collection_value(catalog, prices, cid, prob.tier, prob.stattrak, target)[0]
        if bounds[cid] < 0:
            continue
        l._norm = normalize(l.skin, l.float_value, prob.formula)
        if l._norm > n * min(1.0, bounds[cid]) + 1e-12:
            continue
        cands.append(l)

    # a skin can be in several groups ("3 Redlines" and "7 from the Phoenix collection"): each chosen listing
    # then counts for exactly one of them
    groups_of: dict[str, tuple[int, ...]] = {}
    for gi, g in enumerate(prob.groups):
        for sid in g.skin_ids:
            groups_of[sid] = groups_of.get(sid, ()) + (gi,)
    if prob.groups:
        cands = [l for l in cands if l.skin.id in groups_of]
    cands = _prune(cands, key=lambda l: (l.skin.collection_id, groups_of.get(l.skin.id)), n=n)
    if len(cands) < n:
        return None

    shrink = 0
    for _attempt in range(4):
        res = _solve_once(catalog, prices, prob, cands, bounds, values, groups_of, n, shrink, time_limit)
        if res is None:
            return None
        mx = res.max_output_float
        if mx is None or mx <= target:
            return res
        shrink += 20  # float32 rounding pushed an output over the line: tighten and retry
    return None


def _solve_once(catalog, prices, prob, cands, bounds, values, groups_of, n, shrink, time_limit) -> Result | None:
    m = cp_model.CpModel()
    x = [m.NewBoolVar(f"x{i}") for i in range(len(cands))]
    m.Add(sum(x) == n)

    members: dict[int, list] = defaultdict(list)
    for i, l in enumerate(cands):
        gs = groups_of.get(l.skin.id, ())
        if len(gs) == 1:
            members[gs[0]].append(x[i])
        elif gs:
            assign = [m.NewBoolVar(f"a{i}_{gi}") for gi in gs]
            m.Add(sum(assign) == x[i])
            for gi, a in zip(gs, assign):
                members[gi].append(a)
    for gi, g in enumerate(prob.groups):
        m.Add(sum(members[gi]) >= g.min_count)
        m.Add(sum(members[gi]) <= g.max_count)

    norm_int = [int(round(l._norm * SCALE)) for l in cands]
    total_norm = sum(norm_int[i] * x[i] for i in range(len(cands)))
    by_col = defaultdict(list)
    for i, l in enumerate(cands):
        by_col[l.skin.collection_id].append(i)
    big = n * SCALE
    for cid, idx in by_col.items():
        b = bounds[cid]
        if b >= 1.0:
            continue
        used = m.NewBoolVar(f"used_{cid}")
        for i in idx:
            m.AddImplication(x[i], used)
        cap = int(n * b * SCALE) - shrink
        m.Add(total_norm <= cap + big * (1 - used))

    cost_c = [int(round(l.cost * 100)) for l in cands]
    if prob.max_cost:   # a budget is money to spend: owned skins don't use it up
        m.Add(sum(cost_c[i] * x[i] for i, l in enumerate(cands) if l.market != OWNED) <= int(prob.max_cost * 100))
    if prob.objective == "cost":
        m.Minimize(sum(cost_c[i] * x[i] for i in range(len(cands))))
    else:
        val_c = [int(round(values[l.skin.collection_id] / n * 100)) for l in cands]
        m.Maximize(sum((val_c[i] - cost_c[i]) * x[i] for i in range(len(cands))))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    # yours first, then one block per skin, cheapest first: easy to read and to buy from
    chosen = sorted((cands[i] for i in range(len(cands)) if solver.Value(x[i])),
                    key=lambda l: (l.market != OWNED, l.skin.name, l.souvenir, l.cost, l.float_value))
    inputs = [ContractInput(l.skin, l.float_value, souvenir=l.souvenir, price=l.cost, source=l.market,
                            link=l.link) for l in chosen]
    contract = Contract(catalog, inputs, stattrak=prob.stattrak, formula=prob.formula)
    res = evaluate(contract, prices)
    res.optimal = status == cp_model.OPTIMAL
    res.listings = chosen
    res.problem = prob
    return res


async def confirm_listings(catalog: Catalog, prices: PriceBook, markets: list, results: list[Result],
                           rounds: int = 3) -> tuple[list[Result], int]:
    """Re-check that every chosen listing is still for sale at its price; re-solve around the ones that aren't.

    Returns (results that still work, number of sold/changed listings found). A result whose re-solve
    finds nothing is dropped.
    """
    by_name = {m.name: m for m in markets}
    alive: set[tuple[str, str]] = set()
    dead: set[tuple[str, str]] = set()
    out: list[Result] = []
    for res in results:
        cur = res
        for _ in range(rounds):
            todo: dict[str, list[Listing]] = defaultdict(list)
            for l in cur.listings:
                if (l.market, l.listing_id) not in alive | dead and l.market in by_name:
                    todo[l.market].append(l)
            for market, ls in todo.items():
                gone = await by_name[market].gone(ls)
                for l in ls:
                    (dead if l.listing_id in gone else alive).add((l.market, l.listing_id))
            if not any((l.market, l.listing_id) in dead for l in cur.listings):
                break
            if cur.problem is None:
                cur = None
                break
            prob = cur.problem
            remaining = [l for l in prob.listings if (l.market, l.listing_id) not in dead]
            new = solve(catalog, prices, Problem(prob.tier, prob.stattrak, prob.max_output_float, remaining,
                                                 groups=prob.groups, objective=prob.objective,
                                                 max_cost=prob.max_cost, formula=prob.formula))
            if new is None:
                cur = None
                break
            new.label = cur.label
            cur = new
        else:
            cur = None   # still changing after every round: don't show it
        if cur is not None:
            out.append(cur)
    return out, len(dead)
