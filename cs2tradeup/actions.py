"""The three main jobs (scan, optimize, check), shared by the command line and the guided menu."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime

from .catalog import Catalog, market_hash_name
from .config import Config
from .display import coarse_table, console, progress_text
from .engine import Contract
from .evaluate import Result, evaluate
from .http import Http
from .inventory import fetch_pages, fits, load_owned, resolve_steam_id
from .markets import OWNED, Listing, build_markets
from .optimizer import Group, Problem, confirm_listings, solve
from .pricing import PriceBook
from .scanner import Coverage, ListingFetcher, coarse_candidates, deep_scan, fetch_group_listings, owned_first
from .wear import wear_of


@dataclass
class ScanOutcome:
    results: list[Result] = field(default_factory=list)
    candidates: list = field(default_factory=list)
    deep_ran: bool = False
    requests: int = 0
    errors: list[str] = field(default_factory=list)
    sold: int = 0   # chosen listings that sold or changed price during the scan
    coverage: Coverage | None = None
    confirmed: int = 0   # best results whose listings were re-checked before showing them
    owned: int | None = None   # owned skins usable in the scanned rarities (None = inventory not used)


async def _prices(cfg: Config, http: Http) -> PriceBook:
    pb = PriceBook(cfg)
    with console.status("Loading current prices from Skinport and Waxpeer..."):
        http.prune_cache()
        await pb.load(http)
    return pb


async def _refine(pb: PriceBook, http: Http, results: list[Result]) -> list[Result]:
    """Fetch CSFloat reference prices for outputs that never sold on Skinport, then re-value the results."""
    names = []
    for r in results:
        for v in r.outcomes:
            for var in v.outcome.variants:
                n = market_hash_name(var, v.outcome.wear, r.contract.stattrak)
                if pb.needs_reference(n, var.phase):
                    names.append(n)
    if not names:
        return results
    with console.status(f"Looking up CSFloat reference prices for {len(set(names))} rarely traded result(s)..."):
        await pb.fetch_references(http, names)
    out = []
    for r in results:
        nr = evaluate(r.contract, pb)
        nr.label, nr.listings, nr.optimal, nr.problem, nr.confirmed = (r.label, r.listings, r.optimal,
                                                                       r.problem, r.confirmed)
        out.append(nr)
    return out


async def _owned(cfg: Config, http: Http, cat: Catalog, pb: PriceBook) -> list[Listing]:
    with console.status("Reading your Steam inventory..."):
        owned, _ = await load_owned(http, cfg, cat, pb)
    return owned


def run_inventory(cfg: Config, cat: Catalog, steam_id: str | None = None) -> tuple[list[Listing], int]:
    """(owned skins usable as trade-up inputs, valued as config says; items in the inventory)."""
    async def run():
        http = Http(cfg["rate_limits"])
        try:
            pb = await _prices(cfg, http)
            with console.status("Reading your Steam inventory..."):
                return await load_owned(http, cfg, cat, pb, steam_id)
        finally:
            await http.close()

    return asyncio.run(run())


def check_steam_id(cfg: Config, text: str) -> str:
    """The SteamID64 behind a profile link or name, after making sure its inventory can be read."""
    async def run():
        http = Http(cfg["rate_limits"])
        try:
            sid = await resolve_steam_id(http, text)
            await fetch_pages(http, sid)
            return sid
        finally:
            await http.close()

    return asyncio.run(run())


def markets_status(cfg: Config) -> dict[str, bool]:
    return {
        "CSFloat": bool(cfg.key("csfloat")),
        "DMarket": bool(cfg.key("dmarket_public") and cfg.key("dmarket_secret")),
        "Waxpeer": bool(cfg.key("waxpeer")),
    }


def run_scan(cfg: Config, cat: Catalog, tiers: list[int], modes: list[bool], targets: list[tuple[str, float]],
             souvenir: bool, top: int | None, min_roi: float, coarse_only: bool = False, verbose: bool = False,
             show_coarse: bool = True, time_limit: float | None = None,
             use_inventory: bool = False) -> ScanOutcome:
    """`top` caps how many ideas get a real-listing check (None/0 = all, most promising first);
    `time_limit` (minutes, None/0 = none) stops asking markets after that long. With `use_inventory` the
    skins in the user's Steam inventory are candidates too, and the ideas they fit are checked first."""
    async def run() -> ScanOutcome:
        out = ScanOutcome()
        http = Http(cfg["rate_limits"], verbose=verbose)
        try:
            pb = await _prices(cfg, http)
            out.candidates = coarse_candidates(cat, pb, cfg, tiers, modes, targets, souvenir)
            if show_coarse:
                coarse_table(out.candidates, top or 20)
            if coarse_only or not out.candidates:
                return out
            markets = build_markets(http, cfg)
            if not markets:
                return out
            fetcher = ListingFetcher(markets, log=console.print if verbose else (lambda *a: None), prices=pb)
            owned = []
            if use_inventory:
                owned = [l for l in await _owned(cfg, http, cat, pb)
                         if l.skin.tier in tiers and l.stattrak in modes and (souvenir or not l.souvenir)]
                out.owned = len(owned)
                out.candidates = owned_first(out.candidates, owned, cfg, souvenir)
            ideas = out.candidates[:top] if top else out.candidates
            deadline = time.time() + time_limit * 60 if time_limit else None
            with console.status(f"Checking real listings for {len(ideas)} ideas...") as status:
                found, out.coverage = await deep_scan(
                    cat, pb, cfg, fetcher, ideas, souvenir, log=console.print if verbose else (lambda *a: None),
                    deadline=deadline, progress=lambda cov: status.update(progress_text(cov, deadline)),
                    owned=owned)
            out.deep_ran = True
            out.coverage.owned_first = bool(owned)
            found = sorted(found, key=lambda r: -r.profit)
            # listings sell while the scan runs (and cached ones may be up to listing_cache_minutes old):
            # re-check the best few. CSFloat checks cost one request per listing, so not every result is re-checked.
            shown = int(cfg["scan"].get("confirm_top", 3))
            with console.status("Checking that the chosen listings are still for sale..."):
                confirmed, out.sold = await confirm_listings(cat, pb, markets, found[:shown])
            for r in confirmed:
                r.confirmed = True
            out.confirmed = len(confirmed)
            found = sorted(await _refine(pb, http, confirmed + found[shown:]), key=lambda r: -r.profit)
            out.results = [r for r in found if r.roi >= min_roi]
            out.errors = fetcher.errors
            out.requests = http.requests_made
            return out
        finally:
            await http.close()

    return asyncio.run(run())


def run_recheck(cfg: Config, cat: Catalog, res: Result) -> tuple[Result | None, int]:
    """Re-check one result's listings right before buying (sold ones are replaced). Returns (result, sold)."""
    async def run():
        http = Http(cfg["rate_limits"])
        try:
            pb = await _prices(cfg, http)
            with console.status("Checking that these listings are still for sale..."):
                confirmed, sold = await confirm_listings(cat, pb, build_markets(http, cfg), [res])
            if not confirmed:
                return None, sold
            new = (await _refine(pb, http, confirmed))[0]
            new.confirmed = True
            return new, sold
        finally:
            await http.close()

    return asyncio.run(run())


def limited_note(fetcher: ListingFetcher, markets: list) -> str:
    """' CSFloat's request limit ...' for markets that skipped skins because their limit was used up."""
    note = ""
    for name, n in fetcher.limited_counts().items():
        m = next(m for m in markets if m.name == name)
        reset = m.resume_at()
        when = f", it resets at {datetime.fromtimestamp(reset):%H:%M}" if reset else ""
        note += f" {name}'s request limit is used up{when}, so {n} skin(s) were searched without it."
    return note


def run_optimize(cfg: Config, cat: Catalog, groups: list[Group], tier: int, stattrak: bool, max_out: float,
                 souvenir: bool, objective: str = "cost", verbose: bool = False,
                 use_inventory: bool = False) -> tuple[Result | None, str]:
    """Returns (result, message). result is None when nothing fits; message says why.
    With `use_inventory` the user's own skins of these groups are candidates too."""
    async def run():
        http = Http(cfg["rate_limits"], verbose=verbose)
        try:
            markets = build_markets(http, cfg)
            if not markets and not use_inventory:
                return None, "No market is connected. Add your API keys first (menu option 5)."
            pb = await _prices(cfg, http)
            fetcher = ListingFetcher(markets, log=console.print if verbose else (lambda *a: None), prices=pb)
            with console.status(f"Searching {', '.join(m.name for m in markets)} for listings. "
                                "This can take a minute or two..."):
                ls = await fetch_group_listings(cat, fetcher, groups, tier, stattrak, max_out, cfg, souvenir)
            mine = []
            if use_inventory:
                ids = {sid for g in groups for sid in g.skin_ids}
                mine = [l for l in await _owned(cfg, http, cat, pb)
                        if l.skin.id in ids and fits(l, tier, stattrak, souvenir)]
                ls += mine
            with console.status(f"Finding the best combination among {len(ls)} listings..."):
                res = solve(cat, pb, Problem(tier, stattrak, max_out, ls, groups=groups, objective=objective,
                                             formula=cfg.float_formula))
            if not res:
                msg = (f"Looked at {len(ls)} listings, but no combination reaches that condition. "
                       "Try a worse condition, other skins, or let the program choose how many of each.")
                if fetcher.errors:
                    msg += f" ({len(fetcher.errors)} market request(s) failed, e.g. {fetcher.errors[0]})"
                return None, msg + limited_note(fetcher, markets)
            res.label = f"Best buy for output float <= {max_out:.6f}"
            with console.status("Checking that the chosen listings are still for sale..."):
                confirmed, sold = await confirm_listings(cat, pb, markets, [res])
            if not confirmed:
                return None, (f"The best listings sold while searching ({sold} gone), and no other combination "
                              "reaches that condition right now. Try again in a moment.")
            res = (await _refine(pb, http, confirmed))[0]
            res.confirmed = True
            note = f" {sold} listing(s) had sold or changed price and were replaced." if sold else ""
            if use_inventory:
                note += f" {len(mine)} of your own skins could go into it."
            note += limited_note(fetcher, markets)
            if fetcher.errors:
                note += (f" Warning: {len(fetcher.errors)} market request(s) failed, so a cheaper set may exist "
                         f"(e.g. {fetcher.errors[0]}).")
            return res, f"Checked {len(ls)} listings with {http.requests_made} requests.{note}"
        finally:
            await http.close()

    return asyncio.run(run())


@dataclass
class Check:
    name: str
    status: str   # "ok", "fail" or "skip"
    detail: str


DOCTOR_SKIN = "AK-47 | Redline"   # liquid on every market, float 0.10-0.70
DOCTOR_MAX_FLOAT = 0.25           # below the cheap high-float FT listings, so float filtering is exercised


def run_doctor(cfg: Config, cat: Catalog) -> list[Check]:
    """One real request per source, checking that what comes back still parses into sensible data.

    Markets fail silently when their response format changes (listings just stop showing up), so a
    zero count is treated as a failure here.
    """
    import tempfile
    from pathlib import Path

    from .markets import MARKETS

    async def run() -> list[Check]:
        checks = [Check("Item catalog", "ok" if len(cat.collections) > 50 else "fail",
                        f"{len(cat.skins)} skins, {len(cat.collections)} collections")]
        http = Http(cfg["rate_limits"])                      # bulk prices may come from the cache
        live = Http(cfg["rate_limits"], cache_dir=Path(tempfile.mkdtemp(prefix="tradeup-doctor-")))
        try:
            pb = PriceBook(cfg)
            skin = cat.find_skin(DOCTOR_SKIN)
            try:
                await pb.load(http)
                q = pb.quote([skin], "FT", False)
                checks.append(Check("Skinport prices", "ok" if len(pb.sales) > 1000 and q.price else "fail",
                                    f"{len(pb.sales)} sales rows, {len(pb.skinport)} listings rows, "
                                    f"{DOCTOR_SKIN} (FT) valued at {q.price and round(q.price, 2)}"))
            except Exception as e:
                checks.append(Check("Skinport prices", "fail", str(e)[:150]))
            wx = pb.waxpeer.get(market_hash_name(skin, "FT")) or {}
            checks.append(Check("Waxpeer bulk prices", "ok" if len(pb.waxpeer) > 1000 else "fail",
                                f"{len(pb.waxpeer)} items" + (f", cheapest {DOCTOR_SKIN} FT ${wx['min'] / 1000:.2f}"
                                                              if wx.get("min") else "")))
            for name in cfg["markets"]["enabled"]:
                cls = MARKETS.get(name)
                if not cls:
                    checks.append(Check(name, "fail", "unknown market name in config.toml [markets] enabled"))
                    continue
                m = cls(live, cfg)
                if not m.available:
                    checks.append(Check(m.name, "skip", "no API key set"))
                    continue
                try:
                    ls = await m.listings(skin, False, False, DOCTOR_MAX_FLOAT)
                except Exception as e:
                    checks.append(Check(m.name, "fail", f"request failed: {str(e)[:150]}"))
                    continue
                problems = []
                if not ls:
                    problems.append("0 listings parsed (the API format may have changed)")
                if any(not (skin.min_float <= l.float_value <= DOCTOR_MAX_FLOAT) for l in ls):
                    problems.append("floats outside the requested band")
                if any(l.price <= 0 or l.price > 10_000 for l in ls):
                    problems.append("implausible prices")
                if any("Redline" not in l.name for l in ls):
                    problems.append("listings for the wrong skin")
                if ls:
                    try:
                        gone = await m.gone(ls[:2])
                        if len(gone) == 2:
                            problems.append("availability check says fresh listings are gone")
                    except Exception as e:
                        problems.append(f"availability check failed: {str(e)[:100]}")
                cheapest = min((l.price for l in ls), default=None)
                detail = (f"{len(ls)} listings of {DOCTOR_SKIN} at float <= {DOCTOR_MAX_FLOAT}"
                          + (f", cheapest ${cheapest:.2f}" if cheapest else ""))
                q = live.quota.get(m.name)
                if q:
                    from datetime import datetime

                    detail += (f"; {q['remaining']}" + (f" of {q['limit']}" if q["limit"] else "")
                               + " requests left" + (f", resets {datetime.fromtimestamp(q['reset_at']):%H:%M}"
                                                     if q["reset_at"] else ""))
                checks.append(Check(m.name, "fail" if problems else "ok",
                                    "; ".join(problems) + (f" ({detail})" if problems else "") or detail))
            if not cfg.key("steam_id"):
                checks.append(Check("Steam inventory", "skip", "no Steam profile set"))
            else:
                try:
                    owned, total = await load_owned(live, cfg, cat, pb)
                    checks.append(Check("Steam inventory", "ok" if total else "fail",
                                        f"{total} items, {len(owned)} skins usable in trade-ups"
                                        if total else "0 items read (the inventory format may have changed)"))
                except Exception as e:
                    checks.append(Check("Steam inventory", "fail", str(e)[:150]))
            return checks
        finally:
            await http.close()
            await live.close()

    return asyncio.run(run())


def run_check(cfg: Config, contract: Contract) -> Result:
    """Value a contract with known floats. Inputs are priced at the cheapest listing of their condition."""
    async def run():
        http = Http(cfg["rate_limits"])
        try:
            pb = await _prices(cfg, http)
            for inp in contract.inputs:
                if not inp.price and inp.source != OWNED:   # an owned skin set to "free" stays free
                    offer = pb.cheapest(inp.skin, wear_of(inp.float_value), contract.stattrak, inp.souvenir)
                    if offer:
                        inp.price, inp.source = offer.price, f"{offer.market} lowest"
            res = evaluate(contract, pb)
            res.label = "Your trade-up"
            return (await _refine(pb, http, [res]))[0]
        finally:
            await http.close()

    return asyncio.run(run())
