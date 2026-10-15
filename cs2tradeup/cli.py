"""Command line interface.

  tradeup                                 guided menu (recommended for most people)
  tradeup update-data                     download/refresh the item catalog
  tradeup info "Chroma 2"                 show a collection's skins and float caps
  tradeup calc  -i "AK-47 | Redline@0.12x10"            contract calculator
  tradeup optimize -i "AK-47 | Redline=3" -i "col:Huntsman=7" -t MW
  tradeup scan --tier restricted --target FT --top 10
"""
from __future__ import annotations

import re
import sys
from typing import Optional

import typer
from rich.table import Table

from .actions import run_check, run_optimize, run_scan
from .catalog import Catalog
from .config import load_config
from .display import collection_table, console, print_result
from .engine import Contract, ContractError, ContractInput, contract_size
from .scanner import contract_groups
from .wear import parse_target

app = typer.Typer(add_completion=False, help="CS2 trade-up calculator and cross-market profitability scanner. "
                                             "Run without a command for the guided menu.")

TIER_ALIASES = {
    "consumer": 0, "industrial": 1, "milspec": 2, "mil-spec": 2, "restricted": 3,
    "classified": 4, "covert": 5, "knife": 5, "glove": 5, "gloves": 5,
}


def parse_tier(t: str) -> int:
    key = t.strip().lower().replace(" ", "")
    if key.isdigit():
        return int(key)
    if key not in TIER_ALIASES:
        raise typer.BadParameter(f"unknown rarity '{t}', use one of {', '.join(TIER_ALIASES)}")
    return TIER_ALIASES[key]


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context):
    """Without a command, start the guided menu."""
    if ctx.invoked_subcommand is None:
        from .interactive import run
        run()


@app.command()
def menu():
    """Start the guided menu."""
    from .interactive import run
    run()


@app.command("update-data")
def update_data():
    """Download the latest item catalog (skins, collections, cases)."""
    Catalog.download()
    cat = Catalog()
    console.print(f"Catalog updated: {len(cat.skins)} skins, {len(cat.collections)} collections.")


@app.command()
def info(collection: str = typer.Argument(..., help="Collection or case name (partial match)")):
    """Show a collection's skins per rarity with float caps, and its knife/glove pool."""
    collection_table(Catalog().find_collection(collection))


ITEM_RE = re.compile(r"^(?P<name>.+?)@(?P<float>[0-9.]+)(?:x(?P<n>\d+))?$")


@app.command()
def calc(
    item: list[str] = typer.Option(..., "--item", "-i",
                                   help='Input as "Name@float" or "Name@floatxN", e.g. "AK-47 | Redline@0.16x10". '
                                        'Prefix "souvenir:" for a Souvenir input.'),
    stattrak: bool = typer.Option(False, "--stattrak", help="StatTrak contract"),
    no_prices: bool = typer.Option(False, "--no-prices", help="Skip price lookups (works offline)"),
    config: Optional[str] = typer.Option(None, help="Path to config.toml"),
):
    """Evaluate a contract you already have (floats known): odds, output floats, EV."""
    cfg = load_config(config)
    cat = Catalog()
    inputs = []
    for spec in item:
        souv = spec.lower().startswith("souvenir:")
        if souv:
            spec = spec.split(":", 1)[1]
        m = ITEM_RE.match(spec.strip())
        if not m:
            raise typer.BadParameter(f"can't parse '{spec}', expected Name@float or Name@floatxN")
        skin = cat.find_skin(m["name"])
        inputs += [ContractInput(skin, float(m["float"]), souvenir=souv) for _ in range(int(m["n"] or 1))]
    try:
        contract = Contract(cat, inputs, stattrak=stattrak, formula=cfg.float_formula)
    except ContractError as e:
        console.print(f"[red]Invalid contract:[/] {e}")
        raise typer.Exit(1)
    if no_prices:
        console.print(f"Average input value: {contract.average():.6f}")
        t = Table()
        for c in ("Chance", "Output", "Float", "Wear"):
            t.add_column(c)
        for o in contract.outcomes():
            t.add_row(f"{o.probability * 100:.2f}%", o.name,
                      "-" if o.float_value is None else f"{o.float_value:.6f}", o.wear or "-")
        console.print(t)
        return
    res = run_check(cfg, contract)
    console.print("[dim]Inputs are priced at the lowest current listing of their condition.[/]")
    print_result(res)


@app.command()
def optimize(
    input: list[str] = typer.Option(..., "--input", "-i",
                                    help='"Skin=count", "Skin" (any count), "col:Collection=count" or "=min-max"'),
    target: str = typer.Option("FT", "--target", "-t", help="FN/MW/FT/WW/BS or a max float like 0.149999"),
    stattrak: bool = typer.Option(False, "--stattrak", help="StatTrak contract"),
    tier: Optional[str] = typer.Option(None, help="Rarity, needed only when every input is a collection"),
    souvenir: bool = typer.Option(True, "--souvenir/--no-souvenir", help="Allow Souvenir inputs"),
    objective: str = typer.Option("cost", help="cost = cheapest inputs, profit = best expected profit"),
    config: Optional[str] = typer.Option(None, help="Path to config.toml"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Find the cheapest listings across markets that guarantee the target output float."""
    cfg = load_config(config)
    cat = Catalog()
    groups, t = contract_groups(cat, input, parse_tier(tier) if tier else None)
    max_out = parse_target(target, cfg.safety_margin)
    n = contract_size(t)
    if sum(g.min_count for g in groups) > n:
        raise typer.BadParameter(f"group minimums add up to more than the {n} slots of this contract")
    res, msg = run_optimize(cfg, cat, groups, t, stattrak, max_out, souvenir, objective, verbose)
    if not res:
        console.print(f"[red]{msg}[/]")
        raise typer.Exit(1)
    console.print(f"[dim]{msg}[/]")
    print_result(res)


@app.command()
def scan(
    tier: list[str] = typer.Option(["industrial", "milspec", "restricted", "classified"], "--tier",
                                   help="Input rarities to scan; 'covert' = knife/glove contracts"),
    target: list[str] = typer.Option(["FN", "MW", "FT"], "--target", "-t", help="Target wear(s) or max floats"),
    mode: str = typer.Option("both", help="normal | stattrak | both"),
    souvenir: bool = typer.Option(True, "--souvenir/--no-souvenir", help="Allow Souvenir inputs"),
    top: Optional[int] = typer.Option(None, help="How many coarse candidates get a deep listing check"),
    results: Optional[int] = typer.Option(None, help="How many results to print"),
    min_roi: Optional[float] = typer.Option(None, help="Minimum expected ROI, e.g. 0.05"),
    budget: Optional[float] = typer.Option(None, help="Max total contract cost in USD"),
    coarse_only: bool = typer.Option(False, "--coarse-only", help="Only run the bulk-price pass (no API keys needed)"),
    config: Optional[str] = typer.Option(None, help="Path to config.toml"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Scan all collections for profitable contracts, then pick exact listings for the best ones."""
    cfg = load_config(config)
    if budget is not None:
        cfg["scan"]["max_contract_cost"] = budget
    top = top or int(cfg["scan"]["top_candidates"])
    results = results or int(cfg["scan"]["max_results"])
    min_roi = float(cfg["scan"]["min_roi"]) if min_roi is None else min_roi
    tiers = sorted({parse_tier(t) for t in tier})
    mode_map = {"normal": [False], "stattrak": [True], "both": [False, True]}
    if mode.lower() not in mode_map:
        raise typer.BadParameter(f"unknown mode '{mode}', use normal, stattrak or both", param_hint="--mode")
    modes = mode_map[mode.lower()]
    targets = [(t.upper(), parse_target(t, cfg.safety_margin)) for t in target]
    out = run_scan(cfg, Catalog(), tiers, modes, targets, souvenir, top, min_roi, coarse_only, verbose)
    if coarse_only:
        return
    if not out.deep_ran:
        console.print("[yellow]No market API keys configured, so real listings were not checked.[/]")
        return
    if out.errors:
        console.print(f"[yellow]{len(out.errors)} market request(s) failed:[/] " + "; ".join(out.errors[:3]))
    if out.sold:
        console.print(f"[dim]{out.sold} chosen listing(s) sold or changed price during the scan and were replaced.[/]")
    console.print(f"\n[bold]{len(out.results)} contract(s) at or above {min_roi * 100:.1f}% ROI[/] "
                  f"({out.requests} API requests)")
    for i, r in enumerate(out.results[:results], 1):
        print_result(r, i)


@app.command()
def doctor(config: Optional[str] = typer.Option(None, help="Path to config.toml")):
    """Check that every price source and market still answers in the format this program understands."""
    from .actions import run_doctor
    from .display import doctor_table

    checks = run_doctor(load_config(config), Catalog())
    doctor_table(checks)
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(1)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:  # Windows consoles default to cp1252, which can't print ★ or ™
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        app()
    except (KeyError, ValueError) as e:
        # str(KeyError) wraps the message in quotes
        console.print(f"[red]{e.args[0] if isinstance(e, KeyError) and e.args else e}[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
