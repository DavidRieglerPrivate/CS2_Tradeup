"""Automated scanning.

Pass 1 (coarse, cheap): for every rarity, StatTrak/normal and target wear, estimate per collection
  expected output value per input vs. the cheapest bulk price of an input that can satisfy the float
  budget. Uses only the bulk Skinport/Waxpeer price lists, so it costs ~3 requests total.
Pass 2 (deep): for the best candidates, pull individual listings with floats from CSFloat, DMarket
  and Waxpeer and let the optimizer pick the exact inputs (pure contracts and best mixes).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .catalog import COVERT, Catalog, Collection, Skin
from .config import Config
from .engine import contract_size, normalize
from .evaluate import Result
from .markets import Listing, Market
from .optimizer import Group, Problem, collection_bound, collection_value, solve
from .pricing import PriceBook
from .wear import WEAR_BY_CODE, possible_wears

# Price multiplier for a wear tier where only part of the float range fits (x(1 + 3*unusable share)).
PARTIAL_PENALTY = 3.0
# Buying 10 inputs means climbing past the single cheapest listing. Measured on 2026-10-15 over 18 live
# contracts (Mil-Spec to Classified, cheapest real fill vs this estimate): whole-tier fits cost a median
# 1.29x the cheapest listing (all 6 between 1.18 and 1.31), partial fits 1.10x on top of PARTIAL_PENALTY.
FULL_TIER_MARKUP = 1.29
PARTIAL_MARKUP = 1.10


@dataclass
class Candidate:
    collection: Collection
    tier: int
    stattrak: bool
    target: float          # max output float
    target_label: str
    bound: float           # max average normalized float allowed
    value: float           # EV (net) of a contract made only from this collection
    input_price: float     # estimated price of one input that can satisfy the budget
    input_desc: str
    missing_prices: int

    @property
    def size(self) -> int:
        return contract_size(self.tier)

    @property
    def cost(self) -> float:
        return self.input_price * self.size

    @property
    def margin(self) -> float:
        return self.value - self.cost

    @property
    def roi(self) -> float:
        return self.margin / self.cost if self.cost else 0.0


def tier_input_skins(col: Collection, tier: int, stattrak: bool) -> list[Skin]:
    return [s for s in col.tiers.get(tier, []) if not stattrak or s.stattrak]


def coarse_candidates(catalog: Catalog, prices: PriceBook, cfg: Config, tiers: list[int],
                      stattrak_modes: list[bool], targets: list[tuple[str, float]],
                      include_souvenir: bool) -> list[Candidate]:
    out = []
    for tier in tiers:
        for st in stattrak_modes:
            for label, target in targets:
                for col in catalog.tradeup_collections(tier, st):
                    bound = collection_bound(catalog, col.id, tier, st, target)
                    if bound < 0:
                        continue
                    value, missing = collection_value(catalog, prices, col.id, tier, st, target)
                    if value <= 0:
                        continue
                    best = None
                    for s in tier_input_skins(col, tier, st):
                        variants = [False] + ([True] if include_souvenir and not st and s.souvenir else [])
                        for souv in variants:
                            for w in possible_wears(s.min_float, s.max_float):
                                lo = max(WEAR_BY_CODE[w][2], s.min_float)
                                hi = min(WEAR_BY_CODE[w][3], s.max_float)
                                n_lo = normalize(s, lo, cfg.float_formula)
                                n_hi = normalize(s, hi, cfg.float_formula)
                                if n_lo > bound:
                                    continue
                                offer = prices.cheapest(s, w, st, souv)
                                if not offer:
                                    continue
                                price, note = offer.price * FULL_TIER_MARKUP, ""
                                if n_hi > min(1.0, bound) + 1e-9:
                                    # only the low end of this wear tier fits: the tier's cheapest listing
                                    # probably isn't low enough, so penalize by how much of the tier is unusable
                                    usable = (bound - n_lo) / (n_hi - n_lo) if n_hi > n_lo else 1.0
                                    price = offer.price * (1 + PARTIAL_PENALTY * (1 - usable)) * PARTIAL_MARKUP
                                    note = " partial"
                                if best is None or price < best[0]:
                                    tag = "Souvenir " if souv else ""
                                    best = (price, f"{tag}{s.name} ({w}{note}) @{offer.market}")
                    if best is None:
                        continue
                    out.append(Candidate(col, tier, st, target, label, bound, value, best[0], best[1], missing))
    max_cost = float(cfg["scan"].get("max_contract_cost") or 0)
    if max_cost:
        out = [c for c in out if c.cost <= max_cost]
    out.sort(key=lambda c: -c.roi)
    # when float caps make several targets identical (same value, same inputs), keep one
    seen, uniq = set(), []
    for c in out:
        key = (c.collection.id, c.tier, c.stattrak, round(c.value, 2), round(c.input_price, 4))
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


class ListingFetcher:
    """Fetches and caches listings per (skin, stattrak, souvenir), reusing the widest float band."""

    def __init__(self, markets: list[Market], log=print, prices: PriceBook | None = None):
        self.markets = markets
        self.cache: dict[tuple, tuple[float, list[Listing]]] = {}
        self.errors: list[str] = []
        self.log = log
        self.prices = prices
        self.skipped = 0

    async def get(self, skin: Skin, stattrak: bool, souvenir: bool, max_float: float) -> list[Listing]:
        # Most skins have a Souvenir version on paper but nobody selling one. When neither bulk price list
        # shows a Souvenir listing in any wear, don't spend rate-limited requests on every market.
        if souvenir and self.prices is not None and not self.prices.listed_anywhere(skin, stattrak, True):
            self.skipped += 1
            return []
        key = (skin.id, stattrak, souvenir)
        have = self.cache.get(key)
        if have and have[0] >= max_float:
            return [l for l in have[1] if l.float_value <= max_float]
        results = await asyncio.gather(*(m.listings(skin, stattrak, souvenir, max_float) for m in self.markets),
                                       return_exceptions=True)
        listings: list[Listing] = []
        for m, r in zip(self.markets, results):
            if isinstance(r, Exception):
                msg = f"{m.name}: {skin.name}: {r}"
                self.errors.append(msg)
                self.log(f"[yellow]warning[/] {msg}")
            else:
                listings.extend(r)
        self.cache[key] = (max_float, listings)
        return listings


def fetch_band(skin: Skin, bound: float, slack: float, formula: str) -> float:
    """Highest raw input float worth fetching for a given allowed average."""
    if formula == "legacy":
        return min(skin.max_float, bound * slack)
    return skin.min_float + min(1.0, max(0.0, bound) * slack) * skin.float_range


async def listings_for_collection(fetcher: ListingFetcher, col: Collection, tier: int, stattrak: bool,
                                  bound: float, cfg: Config, include_souvenir: bool) -> list[Listing]:
    slack = float(cfg["scan"]["fetch_float_slack"])
    jobs = []
    for s in tier_input_skins(col, tier, stattrak):
        mf = fetch_band(s, bound, slack, cfg.float_formula)
        jobs.append(fetcher.get(s, stattrak, False, mf))
        if include_souvenir and not stattrak and s.souvenir:
            jobs.append(fetcher.get(s, stattrak, True, mf))
    # all skins in parallel; each market's rate limiter keeps its own pace
    return [l for part in await asyncio.gather(*jobs) for l in part]


async def deep_scan(catalog: Catalog, prices: PriceBook, cfg: Config, fetcher: ListingFetcher,
                    candidates: list[Candidate], include_souvenir: bool, log=print) -> list[Result]:
    results: list[Result] = []
    max_cost = float(cfg["scan"].get("max_contract_cost") or 0)
    # pure contracts per candidate
    pools: dict[tuple, list[Listing]] = {}
    # one fetch per (collection, rarity, StatTrak) with the widest float band any candidate needs
    widest: dict[tuple, Candidate] = {}
    for c in candidates:
        k = (c.collection.id, c.tier, c.stattrak)
        if k not in widest or c.bound > widest[k].bound:
            widest[k] = c
    log(f"[dim]fetching listings for {len(widest)} collection(s) in parallel...[/]")
    got = await asyncio.gather(*(listings_for_collection(fetcher, c.collection, c.tier, c.stattrak, c.bound,
                                                         cfg, include_souvenir) for c in widest.values()))
    by_key = dict(zip(widest.keys(), got))
    fetched = [by_key[(c.collection.id, c.tier, c.stattrak)] for c in candidates]
    for i, (c, ls) in enumerate(zip(candidates, fetched), 1):
        log(f"[dim]deep check {i}/{len(candidates)}: {c.collection.name} "
            f"({'ST ' if c.stattrak else ''}tier {c.tier}, target {c.target_label}): {len(ls)} listings[/]")
        pools.setdefault((c.tier, c.stattrak, c.target, c.target_label), []).extend(ls)
        res = solve(catalog, prices, Problem(c.tier, c.stattrak, c.target, ls, max_cost=max_cost,
                                             formula=cfg.float_formula))
        if res:
            res.label = f"{c.collection.name} | target {c.target_label}"
            results.append(res)
    # best free mix across all candidate collections of the same tier/mode/target
    for (tier, st, target, label), ls in pools.items():
        cols = {l.skin.collection_id for l in ls}
        if len(cols) < 2:
            continue
        res = solve(catalog, prices, Problem(tier, st, target, ls, max_cost=max_cost, formula=cfg.float_formula))
        if res:
            res.label = f"Mixed best ({len({i.skin.collection_id for i in res.contract.inputs})} collections) " \
                        f"| target {label}"
            results.append(res)
    # dedupe identical input sets
    seen, uniq = set(), []
    for r in sorted(results, key=lambda r: -r.profit):
        key = tuple(sorted((l.market, l.listing_id) for l in r.listings))
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def contract_groups(catalog: Catalog, specs: list[str], tier_hint: int | None = None) -> tuple[list[Group], int]:
    """Parse manual input specs.

    'AK-47 | Redline=3'            exactly 3 Redlines
    'AK-47 | Redline'              any number of Redlines
    'col:Huntsman=7'               exactly 7 inputs from the Huntsman collection (any skin of the tier)
    'col:Chroma 2 Case=2-5'        between 2 and 5 inputs from that collection
    """
    groups: list[Group] = []
    tiers: set[int] = set()
    col_specs = []
    for spec in specs:
        body, _, cnt = spec.partition("=")
        lo, hi = 0, 10
        if cnt:
            a, _, b = cnt.partition("-")
            lo = int(a)
            hi = int(b) if b else lo
        body = body.strip()
        if body.lower().startswith("col:"):
            col_specs.append((catalog.find_collection(body[4:]), lo, hi))
        else:
            skin = catalog.find_skin(body)
            tiers.add(skin.tier)
            groups.append(Group({skin.id}, lo, hi, skin.name))
    if col_specs:
        tier = tiers.pop() if len(tiers) == 1 else tier_hint
        if tier is None:
            raise ValueError("collection-only inputs need --tier (e.g. --tier mil-spec)")
        tiers.add(tier)
        for col, lo, hi in col_specs:
            ids = {s.id for s in col.tiers.get(tier, [])}
            if not ids:
                raise ValueError(f"{col.name} has no skins of that rarity")
            groups.append(Group(ids, lo, hi, col.name))
    if len(tiers) != 1:
        raise ValueError("all inputs must be the same rarity")
    return groups, tiers.pop()


async def fetch_group_listings(catalog: Catalog, fetcher: ListingFetcher, groups: list[Group], tier: int,
                               stattrak: bool, target: float, cfg: Config, include_souvenir: bool) -> list[Listing]:
    jobs = []
    slack = float(cfg["scan"]["fetch_float_slack"])
    for g in groups:
        for sid in g.skin_ids:
            s = catalog.skins[sid]
            if stattrak and not s.stattrak:
                continue
            bound = collection_bound(catalog, s.collection_id, tier, stattrak, target)
            if bound < 0:
                continue
            mf = fetch_band(s, bound, slack, cfg.float_formula)
            jobs.append(fetcher.get(s, stattrak, False, mf))
            if include_souvenir and not stattrak and s.souvenir:
                jobs.append(fetcher.get(s, stattrak, True, mf))
    return [l for part in await asyncio.gather(*jobs) for l in part]


__all__ = ["Candidate", "coarse_candidates", "deep_scan", "ListingFetcher", "contract_groups",
           "fetch_group_listings", "COVERT"]
