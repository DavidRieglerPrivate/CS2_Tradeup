"""The three main jobs (scan, optimize, check), shared by the command line and the guided menu."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .catalog import Catalog, market_hash_name
from .config import Config
from .display import coarse_table, console
from .engine import Contract
from .evaluate import Result, evaluate
from .http import Http
from .markets import build_markets
from .optimizer import Group, Problem, confirm_listings, solve
from .pricing import PriceBook
from .scanner import ListingFetcher, coarse_candidates, deep_scan, fetch_group_listings
from .wear import wear_of


@dataclass
class ScanOutcome:
    results: list[Result] = field(default_factory=list)
    candidates: list = field(default_factory=list)
    deep_ran: bool = False
    requests: int = 0
    errors: list[str] = field(default_factory=list)
    sold: int = 0   # chosen listings that sold or changed price during the scan


async def _prices(cfg: Config, http: Http) -> PriceBook:
    pb = PriceBook(cfg)
    with console.status("Loading current prices from Skinport and Waxpeer..."):
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
        nr.label, nr.listings, nr.optimal, nr.problem = r.label, r.listings, r.optimal, r.problem
        out.append(nr)
    return out


def markets_status(cfg: Config) -> dict[str, bool]:
    return {
        "CSFloat": bool(cfg.key("csfloat")),
        "DMarket": bool(cfg.key("dmarket_public") and cfg.key("dmarket_secret")),
        "Waxpeer": bool(cfg.key("waxpeer")),
    }


def run_scan(cfg: Config, cat: Catalog, tiers: list[int], modes: list[bool], targets: list[tuple[str, float]],
             souvenir: bool, top: int, min_roi: float, coarse_only: bool = False, verbose: bool = False,
             show_coarse: bool = True) -> ScanOutcome:
    async def run() -> ScanOutcome:
        out = ScanOutcome()
        http = Http(cfg["rate_limits"], verbose=verbose)
        try:
            pb = await _prices(cfg, http)
            out.candidates = coarse_candidates(cat, pb, cfg, tiers, modes, targets, souvenir)
            if show_coarse:
                coarse_table(out.candidates, top)
            if coarse_only or not out.candidates:
                return out
            markets = build_markets(http, cfg)
            if not markets:
                return out
            fetcher = ListingFetcher(markets, log=console.print if verbose else (lambda *a: None), prices=pb)
            with console.status(f"Checking real listings on {', '.join(m.name for m in markets)} for the best "
                                f"{min(top, len(out.candidates))} ideas. Markets limit how fast we can ask, "
                                "so this can take a few minutes..."):
                found = await deep_scan(cat, pb, cfg, fetcher, out.candidates[:top], souvenir,
                                        log=console.print if verbose else (lambda *a: None))
            out.deep_ran = True
            found = sorted(found, key=lambda r: -r.profit)
            # listings sell while the scan runs: re-check the ones about to be shown
            shown = int(cfg["scan"]["max_results"])
            with console.status("Checking that the chosen listings are still for sale..."):
                confirmed, out.sold = await confirm_listings(cat, pb, markets, found[:shown])
            found = sorted(await _refine(pb, http, confirmed + found[shown:]), key=lambda r: -r.profit)
            out.results = [r for r in found if r.roi >= min_roi]
            out.errors = fetcher.errors
            out.requests = http.requests_made
            return out
        finally:
            await http.close()

    return asyncio.run(run())


def run_optimize(cfg: Config, cat: Catalog, groups: list[Group], tier: int, stattrak: bool, max_out: float,
                 souvenir: bool, objective: str = "cost", verbose: bool = False) -> tuple[Result | None, str]:
    """Returns (result, message). result is None when nothing fits; message says why."""
    async def run():
        http = Http(cfg["rate_limits"], verbose=verbose)
        try:
            markets = build_markets(http, cfg)
            if not markets:
                return None, "No market is connected. Add your API keys first (menu option 5)."
            pb = await _prices(cfg, http)
            fetcher = ListingFetcher(markets, log=console.print if verbose else (lambda *a: None), prices=pb)
            with console.status(f"Searching {', '.join(m.name for m in markets)} for listings. "
                                "This can take a minute or two..."):
                ls = await fetch_group_listings(cat, fetcher, groups, tier, stattrak, max_out, cfg, souvenir)
            with console.status(f"Finding the best combination among {len(ls)} listings..."):
                res = solve(cat, pb, Problem(tier, stattrak, max_out, ls, groups=groups, objective=objective,
                                             formula=cfg.float_formula))
            if not res:
                msg = (f"Looked at {len(ls)} listings, but no combination reaches that condition. "
                       "Try a worse condition, other skins, or let the program choose how many of each.")
                if fetcher.errors:
                    msg += f" ({len(fetcher.errors)} market request(s) failed.)"
                return None, msg
            res.label = f"Best buy for output float <= {max_out:.6f}"
            with console.status("Checking that the chosen listings are still for sale..."):
                confirmed, sold = await confirm_listings(cat, pb, markets, [res])
            if not confirmed:
                return None, (f"The best listings sold while searching ({sold} gone), and no other combination "
                              "reaches that condition right now. Try again in a moment.")
            res = (await _refine(pb, http, confirmed))[0]
            note = f" {sold} listing(s) had sold or changed price and were replaced." if sold else ""
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
                checks.append(Check(m.name, "fail" if problems else "ok",
                                    "; ".join(problems) + (f" ({detail})" if problems else "") or detail))
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
                if not inp.price:
                    offer = pb.cheapest(inp.skin, wear_of(inp.float_value), contract.stattrak, inp.souvenir)
                    if offer:
                        inp.price, inp.source = offer.price, f"{offer.market} lowest"
            res = evaluate(contract, pb)
            res.label = "Your trade-up"
            return (await _refine(pb, http, [res]))[0]
        finally:
            await http.close()

    return asyncio.run(run())
