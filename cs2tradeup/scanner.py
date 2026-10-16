"""Automated scanning.

Pass 1 (coarse, cheap): for every rarity, StatTrak/normal and target wear, estimate per collection
  expected output value per input vs. the cheapest bulk price of an input that can satisfy the float
  budget. Uses only the bulk Skinport/Waxpeer price lists, so it costs ~3 requests total.
Pass 2 (deep): for every candidate, most promising first, pull individual listings with floats and let
  the optimizer pick the exact inputs (pure contracts and best mixes). Each input skin is asked on
  CSFloat and DMarket first; Waxpeer (no float filter, so one search per wear) is then searched only
  for the wears where its bulk price list shows something that could beat what was already found.
  The pass stops at a deadline; candidates whose listings weren't all fetched by then are not checked.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from collections import Counter
from dataclasses import dataclass, field

from .catalog import COVERT, Catalog, Collection, Skin, market_hash_name
from .config import Config
from .engine import contract_size, normalize
from .evaluate import Result
from .http import PRIORITY
from .markets import Listing, Market, _wears_in_band
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


SLOW_MARKET = "waxpeer"   # one search per wear: asked last, and only where it can matter


@dataclass
class Coverage:
    """How much of the search space a scan actually looked at."""
    ideas: int = 0
    checked: int = 0                  # ideas whose input skins were all asked on the fast markets
    skins: int = 0
    skins_done: int = 0
    slow_searched: int = 0            # Waxpeer wear searches made (or answered from the cache)
    slow_skipped_none: int = 0        # skipped: Waxpeer's bulk list shows nothing listed in that wear
    slow_skipped_price: int = 0       # skipped: Waxpeer's cheapest costs more than enough listings already found
    slow_unreached: int = 0           # wanted, but the time ran out first
    limited: dict[str, int] = field(default_factory=dict)   # market -> skins it couldn't answer (limit used up)
    timed_out: bool = False
    seconds: float = 0.0
    waiting: tuple[str, float] | None = None   # (market, reset time) while waiting for a request limit
    solving: int = 0                  # set once fetching is over: ideas now being solved


class ListingFetcher:
    """Fetches and caches listings per (skin, stattrak, souvenir), reusing the widest float band."""

    def __init__(self, markets: list[Market], log=print, prices: PriceBook | None = None):
        self.markets = markets
        self.cache: dict[tuple, tuple[float, list[Listing]]] = {}
        self.errors: list[str] = []
        self.log = log
        self.prices = prices
        self.skipped = 0
        self.coverage = Coverage()
        self.on_progress = lambda: None
        self.fast_done: set[tuple] = set()            # keys whose fast markets have answered (or failed)
        self.limited: dict[str, list[tuple]] = {}     # market -> [(skin, stattrak, souvenir, max_float)]
        self.slow_wanted = 0                          # Waxpeer wear searches decided on so far
        self.slow_limited: set[tuple] = set()         # keys that went without Waxpeer (its limit used up)

    def limited_counts(self) -> dict[str, int]:
        """market -> skins checked without it because its request limit was used up."""
        out = {m: len(v) for m, v in self.limited.items() if v}
        if self.slow_limited:
            out[SLOW_MARKET] = len(self.slow_limited)
        return out

    async def _ask(self, markets: list[Market], skin: Skin, stattrak: bool, souvenir: bool, max_float: float,
                   queue_limited: bool = True, **kw) -> tuple[list[Listing], set[str]]:
        """(listings, names of markets whose request limit is used up). Those markets aren't errors: with
        `queue_limited` the skin is queued in `self.limited` to be asked again after the reset."""
        results = await asyncio.gather(*(m.listings(skin, stattrak, souvenir, max_float, **kw) for m in markets),
                                       return_exceptions=True)
        listings: list[Listing] = []
        limited: set[str] = set()
        for m, r in zip(markets, results):
            if isinstance(r, asyncio.CancelledError):
                raise r
            if isinstance(r, Exception):
                if getattr(r, "status", None) == 429 and m.resume_at():
                    limited.add(m.name)
                    if queue_limited:
                        self.limited.setdefault(m.name, []).append((skin, stattrak, souvenir, max_float))
                    continue
                msg = f"{m.name}: {skin.name}: {r}"
                self.errors.append(msg)
                self.log(f"[yellow]warning[/] {msg}")
            else:
                listings.extend(r)
        return listings, limited

    def slow_wears(self, market: Market, skin: Skin, stattrak: bool, souvenir: bool, max_float: float,
                   have: list[Listing], need: int) -> list[str]:
        """Wears worth a Waxpeer search, judged by its bulk price list (cheapest listing and count per wear).

        A wear is skipped when nothing is listed there, or when its cheapest listing costs more than the
        `need`-th cheapest listing of the same skin and wear already found on the other markets: a contract
        uses at most `need` of one skin, so a pricier Waxpeer listing only helps through a lower float.
        """
        wears = _wears_in_band(skin, max_float)
        bulk = self.prices.waxpeer if self.prices is not None else {}
        if not bulk:   # the bulk list didn't load: no basis to skip anything
            return wears
        out = []
        for w in wears:
            row = bulk.get(market_hash_name(skin, w, stattrak, souvenir)) or {}
            if not row.get("count") or not row.get("min"):
                self.coverage.slow_skipped_none += 1
                continue
            lo, hi = max(WEAR_BY_CODE[w][2], skin.min_float), min(WEAR_BY_CODE[w][3], max_float)
            same = sorted(l.cost for l in have if lo <= l.float_value <= hi)
            cheapest = row["min"] / 1000 * (1 + market.buy_fee)
            if len(same) >= need and cheapest >= same[need - 1]:
                self.coverage.slow_skipped_price += 1
                continue
            out.append(w)
        return out

    async def get(self, skin: Skin, stattrak: bool, souvenir: bool, max_float: float,
                  need: int = 10) -> list[Listing]:
        # Most skins have a Souvenir version on paper but nobody selling one. When neither bulk price list
        # shows a Souvenir listing in any wear, don't spend rate-limited requests on every market.
        key = (skin.id, stattrak, souvenir)
        if souvenir and self.prices is not None and not self.prices.listed_anywhere(skin, stattrak, True):
            self.skipped += 1
            self.fast_done.add(key)
            return []
        have = self.cache.get(key)
        if have and have[0] >= max_float:
            return [l for l in have[1] if l.float_value <= max_float]
        fast = [m for m in self.markets if m.name != SLOW_MARKET]
        slow = [m for m in self.markets if m.name == SLOW_MARKET]
        listings, _ = await self._ask(fast, skin, stattrak, souvenir, max_float)
        # stored before the slow market is asked, so a scan that runs out of time still has these
        self.cache[key] = (max_float, listings)
        self.fast_done.add(key)
        self.on_progress()
        for m in slow:
            wears = self.slow_wears(m, skin, stattrak, souvenir, max_float, listings, need)
            self.slow_wanted += len(wears)
            for i, w in enumerate(wears):   # one search at a time: the progress count moves, a deadline loses little
                found, limited = await self._ask([m], skin, stattrak, souvenir, max_float, queue_limited=False,
                                                 wears=[w])
                listings.extend(found)
                if limited:   # its other wears would fail the same way; this skin goes without them
                    self.slow_limited.add(key)
                    self.slow_wanted -= len(wears) - i
                    break
                self.coverage.slow_searched += 1
                self.on_progress()
        return listings

    async def retry_limited(self, deadline: float | None, fetching: asyncio.Event | None = None) -> None:
        """Ask the skins a used-up request limit made us skip again, once the limit resets before `deadline`.

        Runs alongside the fetch: until `fetching` is set, more skipped skins can still arrive.
        """
        by_name = {m.name: m for m in self.markets}
        while True:
            queued = [n for n, jobs in self.limited.items() if jobs]
            if not queued:
                if fetching is None or fetching.is_set():
                    return
                await asyncio.sleep(0.5)
                continue
            # the market that can answer soonest
            name = min(queued, key=lambda n: by_name[n].resume_at() or 0.0)
            m = by_name[name]
            reset = m.resume_at()
            if reset and deadline is not None and reset >= deadline:
                if fetching is None or fetching.is_set():
                    return
                await asyncio.sleep(0.5)   # nothing to retry in time, but the fetch may still queue others
                continue
            if reset:
                self.coverage.waiting = (name, reset)
                self.on_progress()
                await asyncio.sleep(reset - time.time() + 1)
                self.coverage.waiting = None
            jobs = self.limited[name]
            # a job leaves the queue only once answered, so a deadline or cancellation can't lose it
            while jobs:
                if deadline is not None and time.time() >= deadline:
                    return
                skin, stattrak, souvenir, max_float = jobs[0]
                found, limited = await self._ask([m], skin, stattrak, souvenir, max_float, queue_limited=False)
                if limited:   # used up again: the rest waits for the next reset
                    break
                jobs.pop(0)
                self.cache.setdefault((skin.id, stattrak, souvenir), (max_float, []))[1].extend(found)
                self.on_progress()
            if not jobs:
                self.limited.pop(name, None)


def fetch_band(skin: Skin, bound: float, slack: float, formula: str) -> float:
    """Highest raw input float worth fetching for a given allowed average."""
    if formula == "legacy":
        return min(skin.max_float, bound * slack)
    return skin.min_float + min(1.0, max(0.0, bound) * slack) * skin.float_range


def skin_jobs(col: Collection, tier: int, stattrak: bool, bound: float, cfg: Config,
              include_souvenir: bool) -> list[tuple[Skin, bool, float]]:
    """(skin, souvenir, highest float to fetch) for every input skin version of one collection."""
    slack = float(cfg["scan"]["fetch_float_slack"])
    out = []
    for s in tier_input_skins(col, tier, stattrak):
        mf = fetch_band(s, bound, slack, cfg.float_formula)
        out.append((s, False, mf))
        if include_souvenir and not stattrak and s.souvenir:
            out.append((s, True, mf))
    return out


def _solve_all(catalog: Catalog, prices: PriceBook, cfg: Config, candidates: list[Candidate],
               pool_of: dict[tuple, list[Listing]], log) -> list[Result]:
    results: list[Result] = []
    max_cost = float(cfg["scan"].get("max_contract_cost") or 0)
    pools: dict[tuple, list[Listing]] = {}
    for i, c in enumerate(candidates, 1):
        ls = pool_of[(c.collection.id, c.tier, c.stattrak)]
        log(f"[dim]deep check {i}/{len(candidates)}: {c.collection.name} "
            f"({'ST ' if c.stattrak else ''}tier {c.tier}, target {c.target_label}): {len(ls)} listings[/]")
        pools.setdefault((c.tier, c.stattrak, c.target, c.target_label), []).extend(ls)
        res = solve(catalog, prices, Problem(c.tier, c.stattrak, c.target, ls, max_cost=max_cost,
                                             formula=cfg.float_formula))
        if res:
            res.label = f"{c.collection.name} | target {c.target_label}"
            results.append(res)
    # best free mix across all checked collections of the same tier/mode/target
    for (tier, st, target, label), ls in pools.items():
        if len({l.skin.collection_id for l in ls}) < 2:
            continue
        res = solve(catalog, prices, Problem(tier, st, target, ls, max_cost=max_cost, formula=cfg.float_formula))
        used = len({i.skin.collection_id for i in res.contract.inputs}) if res else 0
        if used >= 2:   # a "mix" from one collection is that collection's own contract, already listed
            res.label = f"Mixed best ({used} collections) | target {label}"
            results.append(res)
    # dedupe identical input sets
    seen, uniq = set(), []
    for r in sorted(results, key=lambda r: -r.profit):
        key = tuple(sorted((l.market, l.listing_id) for l in r.listings))
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


async def deep_scan(catalog: Catalog, prices: PriceBook, cfg: Config, fetcher: ListingFetcher,
                    candidates: list[Candidate], include_souvenir: bool, log=print,
                    deadline: float | None = None, progress=None) -> tuple[list[Result], Coverage]:
    """Fetch listings for the candidates (in the given order) until `deadline`, then solve every checked one.

    `progress(coverage)` is called whenever the counts change.
    """
    started = time.time()
    cov = fetcher.coverage
    cov.ideas = len(candidates)
    # one fetch per (collection, rarity, StatTrak) with the widest float band any candidate needs
    widest: dict[tuple, Candidate] = {}
    for c in candidates:
        k = (c.collection.id, c.tier, c.stattrak)
        if k not in widest or c.bound > widest[k].bound:
            widest[k] = c
    jobs = {k: skin_jobs(c.collection, c.tier, c.stattrak, c.bound, cfg, include_souvenir)
            for k, c in widest.items()}
    cov.skins = sum(len(j) for j in jobs.values())
    done_keys = lambda k: [(s.id, k[2], souv) for s, souv, _ in jobs[k]]
    ideas_of = Counter((c.collection.id, c.tier, c.stattrak) for c in candidates)
    complete = lambda k: all(sk in fetcher.fast_done for sk in done_keys(k))

    def update():
        cov.skins_done = len(fetcher.fast_done)
        cov.checked = sum(n for k, n in ideas_of.items() if complete(k))
        cov.slow_unreached = max(0, fetcher.slow_wanted - cov.slow_searched)
        cov.limited = fetcher.limited_counts()
        cov.seconds = time.time() - started
        if progress:
            progress(cov)

    fetcher.on_progress = update
    log(f"[dim]fetching listings for {len(widest)} collection(s), {cov.skins} skin(s)...[/]")
    # every request of a skin carries its collection's rank, so the rate limiters serve the most promising
    # ideas first, follow-up pages and Waxpeer searches included
    tasks = []
    for rank, k in enumerate(widest):
        ctx = contextvars.copy_context()
        ctx.run(PRIORITY.set, rank)
        tasks += [asyncio.create_task(fetcher.get(s, k[2], souv, mf, need=contract_size(k[1])), context=ctx)
                  for s, souv, mf in jobs[k]]
    if tasks:
        fetching = asyncio.Event()

        async def fetch_all():
            await asyncio.wait(tasks)
            fetching.set()

        # skins skipped for a used-up limit are asked again after its reset, while the other markets go on
        work = [asyncio.create_task(fetch_all()), asyncio.create_task(fetcher.retry_limited(deadline, fetching))]
        timeout = None if deadline is None else max(0.0, deadline - time.time())
        await asyncio.wait(work, timeout=timeout)
        pending = [t for t in tasks + work if not t.done()]
        cov.timed_out = any(not t.done() for t in tasks)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in tasks + work:   # a bug in one skin's fetch must not vanish silently
            if not t.cancelled() and t.exception() is not None:
                fetcher.errors.append(f"internal error: {t.exception()!r}")
                log(f"[red]internal error[/] {t.exception()!r}")
    checked = [c for c in candidates if complete((c.collection.id, c.tier, c.stattrak))]
    cov.solving = len(checked)
    update()
    pool_of = {k: [l for sk in done_keys(k) for l in fetcher.cache.get(sk, (0, []))[1]] for k in widest}
    return _solve_all(catalog, prices, cfg, checked, pool_of, log), cov


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
            jobs.append(fetcher.get(s, stattrak, False, mf, need=contract_size(tier)))
            if include_souvenir and not stattrak and s.souvenir:
                jobs.append(fetcher.get(s, stattrak, True, mf, need=contract_size(tier)))
    return [l for part in await asyncio.gather(*jobs) for l in part]


__all__ = ["Candidate", "coarse_candidates", "deep_scan", "ListingFetcher", "contract_groups",
           "fetch_group_listings", "COVERT"]
