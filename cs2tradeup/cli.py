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

from .actions import run_check, run_inventory, run_optimize, run_scan
from .catalog import Catalog, split_item_name
from .config import load_config
from .display import collection_table, console, coverage_lines, print_result
from .engine import Contract, ContractError, ContractInput
from .scanner import check_groups, contract_groups
from .wear import parse_target

app = typer.Typer(add_completion=False, help="CS2 trade-up calculator and cross-market profitability scanner. "
                                             "Run without a command for the guided menu.")

INVENTORY_HELP = ("Also use skins you already own (Steam inventory of steam_id in config.toml), "
                  "each counted at what it would sell for")

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


ITEM_RE = re.compile(r"^(?P<name>.+?)\s*@\s*(?P<float>\d*[.,]?\d+)\s*(?:[x×*]\s*(?P<n>\d+))?$", re.I)


@app.command()
def calc(
    item: list[str] = typer.Option(..., "--item", "-i",
                                   help='Input as "Name@float" or "Name@floatxN", e.g. "AK-47 | Redline@0.16x10". '
                                        'Prefix "souvenir:" (or "Souvenir ") for a Souvenir input, "StatTrak™ " '
                                        'for StatTrak; Steam market names with a wear in brackets work too.'),
    stattrak: bool = typer.Option(False, "--stattrak", help="StatTrak contract"),
    no_prices: bool = typer.Option(False, "--no-prices", help="Skip price lookups (works offline)"),
    config: Optional[str] = typer.Option(None, help="Path to config.toml"),
):
    """Evaluate a contract you already have (floats known): odds, output floats, EV."""
    cfg = load_config(config)
    cat = Catalog()
    inputs = []
    marked_st = []
    for spec in item:
        m = ITEM_RE.match(spec.strip())
        if not m:
            raise typer.BadParameter(f"can't parse '{spec}', expected Name@float or Name@floatxN "
                                     f"(e.g. \"AK-47 | Redline@0.16x10\")")
        name, st, souv, _ = split_item_name(m["name"])
        skin = cat.find_skin(name)
        count = int(m["n"] or 1)
        inputs += [ContractInput(skin, float(m["float"].replace(",", ".")), souvenir=souv) for _ in range(count)]
        marked_st += [st] * count
    if any(marked_st) and not all(marked_st):
        console.print("[red]Invalid contract:[/] StatTrak™ and normal inputs can't be mixed")
        raise typer.Exit(1)
    stattrak = stattrak or all(marked_st)
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
    inventory: bool = typer.Option(False, "--inventory", help=INVENTORY_HELP),
    config: Optional[str] = typer.Option(None, help="Path to config.toml"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Find the cheapest listings across markets that guarantee the target output float."""
    cfg = load_config(config)
    cat = Catalog()
    groups, t = contract_groups(cat, input, parse_tier(tier) if tier else None)
    max_out = parse_target(target, cfg.safety_margin)
    check_groups(cat, groups, t, stattrak)
    res, msg = run_optimize(cfg, cat, groups, t, stattrak, max_out, souvenir, objective, verbose,
                            use_inventory=inventory)
    if not res:
        console.print(f"[red]{msg}[/]")
        raise typer.Exit(1)
    console.print(f"[dim]{msg}[/]")
    print_result(res)


def scan_settings(cfg, tier: list[str], mode: str, target: list[str]):
    """(tiers, StatTrak modes, targets) from the scan/watch options."""
    tiers = sorted({parse_tier(t) for t in tier})
    mode_map = {"normal": [False], "stattrak": [True], "both": [False, True]}
    if mode.lower() not in mode_map:
        raise typer.BadParameter(f"unknown mode '{mode}', use normal, stattrak or both", param_hint="--mode")
    targets = [(t.upper(), parse_target(t, cfg.safety_margin)) for t in target]
    return tiers, mode_map[mode.lower()], targets


@app.command()
def scan(
    tier: list[str] = typer.Option(["industrial", "milspec", "restricted", "classified"], "--tier",
                                   help="Input rarities to scan; 'covert' = knife/glove contracts"),
    target: list[str] = typer.Option(["FN", "MW", "FT"], "--target", "-t", help="Target wear(s) or max floats"),
    mode: str = typer.Option("both", help="normal | stattrak | both"),
    souvenir: bool = typer.Option(True, "--souvenir/--no-souvenir", help="Allow Souvenir inputs"),
    top: Optional[int] = typer.Option(None, help="Only check the best N ideas on real listings (default: all)"),
    time: Optional[float] = typer.Option(None, "--time",
                                         help="Minutes to spend asking markets (0 = no limit, default 10)"),
    results: Optional[int] = typer.Option(None, help="How many results to print"),
    min_roi: Optional[float] = typer.Option(None, help="Minimum expected ROI, e.g. 0.05"),
    budget: Optional[float] = typer.Option(None, help="Max total contract cost in USD"),
    coarse_only: bool = typer.Option(False, "--coarse-only", help="Only run the bulk-price pass (no API keys needed)"),
    inventory: bool = typer.Option(False, "--inventory", help=INVENTORY_HELP),
    config: Optional[str] = typer.Option(None, help="Path to config.toml"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Scan all collections for profitable contracts, then pick exact listings for the best ones."""
    cfg = load_config(config)
    if budget is not None:
        cfg["scan"]["max_contract_cost"] = budget
    top = int(cfg["scan"]["top_candidates"]) if top is None else top
    time = float(cfg["scan"]["time_limit_minutes"]) if time is None else time
    results = results or int(cfg["scan"]["max_results"])
    min_roi = float(cfg["scan"]["min_roi"]) if min_roi is None else min_roi
    tiers, modes, targets = scan_settings(cfg, tier, mode, target)
    out = run_scan(cfg, Catalog(), tiers, modes, targets, souvenir, top, min_roi, coarse_only, verbose,
                   time_limit=time, use_inventory=inventory and not coarse_only)
    if coarse_only:
        return
    if not out.deep_ran:
        console.print("[yellow]No market API keys configured, so real listings were not checked.[/]")
        return
    if out.owned is not None:
        console.print(f"{out.owned} of your own skins fit these settings; ideas they can go into were checked first.")
    for line in coverage_lines(out.coverage):
        console.print(line)
    if out.errors:
        console.print(f"[yellow]{len(out.errors)} market request(s) failed:[/] " + "; ".join(out.errors[:3]))
    if out.sold:
        console.print(f"[dim]{out.sold} chosen listing(s) sold or changed price during the scan and were replaced.[/]")
    console.print(f"\n[bold]{len(out.results)} contract(s) at or above {min_roi * 100:.1f}% ROI[/] "
                  f"({out.requests} API requests)")
    for i, r in enumerate(out.results[:results], 1):
        print_result(r, i)


@app.command()
def watch(
    every: float = typer.Option(60, help="Minutes between scans"),
    tier: list[str] = typer.Option(["milspec", "restricted", "classified"], "--tier", help="Input rarities to scan"),
    target: list[str] = typer.Option(["MW", "FT"], "--target", "-t", help="Target wear(s) or max floats"),
    mode: str = typer.Option("normal", help="normal | stattrak | both"),
    souvenir: bool = typer.Option(True, "--souvenir/--no-souvenir", help="Allow Souvenir inputs"),
    top: int = typer.Option(0, help="Only check the best N ideas on real listings per scan (0 = all)"),
    time: float = typer.Option(15, "--time", help="Minutes each scan may spend asking markets (0 = no limit)"),
    min_roi: float = typer.Option(0.05, help="Alert only at or above this expected ROI"),
    budget: Optional[float] = typer.Option(None, help="Max total contract cost in USD"),
    once: bool = typer.Option(False, "--once", help="Run one scan and exit (for Windows Task Scheduler)"),
    inventory: bool = typer.Option(False, "--inventory", help=INVENTORY_HELP),
    config: Optional[str] = typer.Option(None, help="Path to config.toml"),
):
    """Scan again every few minutes and send a notification when a new profitable contract shows up."""
    from .watch import watch as run_watch

    tiers, modes, targets = scan_settings(load_config(config), tier, mode, target)
    try:
        run_watch(lambda: load_config(config), tiers, modes, targets, souvenir, top, min_roi, every,
                  once=once, budget=budget, time_limit=time, use_inventory=inventory)
    except KeyboardInterrupt:
        console.print("\nStopped watching.")


@app.command("inventory")
def inventory_cmd(
    steam_id: Optional[str] = typer.Option(None, "--steam-id",
                                           help="Profile link, SteamID64 or profile name (default: steam_id in "
                                                "config.toml)"),
    config: Optional[str] = typer.Option(None, help="Path to config.toml"),
):
    """List the skins in your Steam inventory that can go into a trade-up, with floats and values."""
    from .display import inventory_table

    cfg = load_config(config)
    cat = Catalog()
    owned, total = run_inventory(cfg, cat, steam_id)
    inventory_table(cat, owned)
    free = str(cfg["inventory"].get("owned_cost", "sell")).lower() == "free"
    console.print(f"{len(owned)} of {total} items can go into a trade-up. Worth = "
                  + ("0, owned_cost is \"free\" in config.toml." if free else "what each would sell for after the "
                     "sell fee; that's what using it in a trade-up costs you."))


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
    from .inventory import InventoryError

    try:
        app()
    except (KeyError, ValueError, InventoryError) as e:
        # str(KeyError) wraps the message in quotes
        console.print(f"[red]{e.args[0] if isinstance(e, KeyError) and e.args else e}[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
