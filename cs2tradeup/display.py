"""Everything that prints to the terminal."""
from __future__ import annotations

import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .catalog import COVERT, TIER_NAMES, Collection, market_hash_name
from .evaluate import Result
from .wear import possible_wears

console = Console(highlight=False, width=None if sys.stdout.isatty() else 200)

TIER_COLORS = ["grey70", "cyan", "blue", "magenta", "bright_magenta", "red", "yellow"]


def tier_label(tier: int) -> str:
    return f"[{TIER_COLORS[tier]}]{TIER_NAMES[tier]}[/]"


def money(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def signed_money(v: float) -> str:
    return f"+${v:,.2f}" if v >= 0 else f"-${-v:,.2f}"


def pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def contract_kind(res: Result) -> str:
    c = res.contract
    st = "StatTrak™ " if c.stattrak else ""
    if c.tier == COVERT:
        return f"{st}5x Covert -> Knife/Gloves"
    return f"{st}{c.size}x {TIER_NAMES[c.tier]} -> {TIER_NAMES[c.tier + 1]}"


def verdict(res: Result) -> tuple[str, str]:
    """(short verdict with colour, one-sentence explanation)."""
    if res.missing_prices:
        note = " Some possible results have no price data, so the real value may be higher."
    else:
        note = ""
    if res.profit > 0 and res.profit_chance >= 0.5:
        return "[bold green]Looks profitable[/]", "You make money on average, and in most cases." + note
    if res.profit > 0:
        return ("[bold yellow]Risky gamble[/]",
                "You make money on average, but most single attempts lose money. It only pays off "
                "if you repeat it many times." + note)
    return "[bold red]Not worth it[/]", "You lose money on average." + note


def explain(res: Result) -> None:
    """Plain-language summary for people who don't want to read the tables."""
    c = res.contract
    best = max(res.outcomes, key=lambda v: v.quote.net or 0)
    worst = min(res.outcomes, key=lambda v: v.quote.net or 0)
    v_short, v_long = verdict(res)
    lines = [
        f"Verdict: {v_short}",
        v_long,
        "",
        f"* You buy {c.size} skins for [bold]{money(res.cost)}[/] in total.",
        f"* You get [bold]one[/] random skin back. On average it sells for [bold]{money(res.ev)}[/] "
        f"after the {res.sell_fee * 100:g}% fee for selling"
        + (f" on {res.sell_market}" if res.sell_market else "") + ".",
        f"* Average result per trade-up: [{'green' if res.profit >= 0 else 'red'}]{signed_money(res.profit)} "
        f"({pct(res.roi)})[/].",
        f"* Chance that the skin you get is worth more than you paid: [bold]{res.profit_chance * 100:.0f}%[/].",
        f"* Best case: {market_hash_name(best.outcome.skin, None, c.stattrak)} "
        f"({best.outcome.probability * 100:.1f}% chance), {signed_money((best.quote.net or 0) - res.cost)}.",
        f"* Worst case: {market_hash_name(worst.outcome.skin, None, c.stattrak)} "
        f"({worst.outcome.probability * 100:.1f}% chance), {signed_money((worst.quote.net or 0) - res.cost)}.",
    ]
    if len(res.outcomes) > 1:
        half = ("." if not res.big_loss_chance
                else ", and you always get back less than half of what you paid." if res.big_loss_chance > 0.999
                else f", and {res.big_loss_chance * 100:.0f}% of the time you get back less than half.")
        lines.append(f"* One result typically lands [bold]{money(res.spread)}[/] above or below that average" + half)
    if res.runs_needed and res.runs_needed > 1:
        lines.append(f"* To be about 95% sure of being ahead overall, plan on [bold]{res.runs_needed}[/] of these "
                     f"(about {money(res.runs_needed * res.cost)} spent in total).")
    lines.append(f"[dim]* Values are Skinport sale prices. If you sell somewhere else, change sell_fee and "
                 f"sell_market in config.toml (CSFloat 2%, Skinport 8%, Steam 15%).[/]")
    if any(v.quote.illiquid for v in res.outcomes):
        lines.append("[yellow]* Results marked with ! sell rarely, so their price is less certain.[/]")
    console.print(Panel("\n".join(lines), title="What this means", title_align="left", border_style="cyan"))


def print_result(res: Result, rank: int | None = None, plain: bool = True) -> None:
    c = res.contract
    head = f"#{rank} " if rank else ""
    console.rule(f"[bold]{head}{res.label or 'Trade-up'}[/]  {contract_kind(res)}")
    if plain:
        explain(res)
    else:
        colour = "green" if res.profit > 0 else "red"
        console.print(
            f"Cost [bold]{money(res.cost)}[/]   EV (after sell fee) [bold]{money(res.ev)}[/]   "
            f"Profit [{colour}]{money(res.profit)} ({pct(res.roi)})[/]   "
            f"Chance of profit [bold]{res.profit_chance * 100:.1f}%[/]   "
            f"Worst {money(res.worst)}   Best {money(res.best)}")
    console.print(f"[dim]Average {'normalized ' if c.formula == 'normalized' else ''}input float: "
                  f"{c.average():.6f}[/]"
                  + ("" if res.optimal else "   [yellow](search time limit hit, may not be the very best)[/]"))
    if res.missing_prices:
        console.print(f"[yellow]{res.missing_prices} possible result(s) have no price data and count as $0.[/]")

    t = Table(title="What you can get", title_justify="left")
    for col in ("Chance", "Skin you get", "Float", "Condition", "Sells for", "You receive", "Sales", "Price source"):
        t.add_column(col, justify="right" if col in ("Chance", "Float", "Sells for", "You receive", "Sales")
                     else "left")
    for v in res.outcomes:
        o, q = v.outcome, v.quote
        net_col = "green" if (q.net or 0) > res.cost else "red"
        t.add_row(f"{o.probability * 100:.2f}%", market_hash_name(o.skin, None, c.stattrak),
                  "-" if o.float_value is None else f"{o.float_value:.6f}", o.wear or "-",
                  money(q.price), f"[{net_col}]{money(q.net)}[/]",
                  f"{q.volume}{' !' if q.illiquid else ''}", q.source)
    console.print(t)

    has_links = any(i.link for i in c.inputs)
    t = Table(title="Skins to buy" if has_links else "Your skins (valued at the cheapest listing)",
              title_justify="left")
    for col in ("#", "Market", "Skin", "Float", "Price"):
        t.add_column(col, justify="right" if col in ("#", "Float", "Price") else "left")
    for i, inp in enumerate(c.inputs, 1):
        name = ("Souvenir " if inp.souvenir else "StatTrak™ " if c.stattrak else "") + inp.skin.name
        t.add_row(str(i), inp.source or "-", name, f"{inp.float_value:.6f}", money(inp.price))
    console.print(t)
    if has_links:
        console.print("[bold]Buy links[/] (Ctrl+click to open):")
        for i, inp in enumerate(c.inputs, 1):
            if inp.link:
                console.print(f"  {i:>2}. {inp.link}  (float {inp.float_value:.6f}, {money(inp.price)})")


def results_overview(results: list[Result]) -> None:
    t = Table(title="Trade-ups found (best first)", title_justify="left")
    for col in ("#", "Trade-up", "Type", "Cost", "Avg return", "Avg profit", "Win chance", "Swing", "Verdict"):
        t.add_column(col, justify="right" if col in ("#", "Cost", "Avg return", "Avg profit", "Win chance", "Swing")
                     else "left")
    for i, r in enumerate(results, 1):
        t.add_row(str(i), r.label, contract_kind(r), money(r.cost), money(r.ev),
                  f"[{'green' if r.profit >= 0 else 'red'}]{signed_money(r.profit)} ({pct(r.roi)})[/]",
                  f"{r.profit_chance * 100:.0f}%", f"±{money(r.spread)}", verdict(r)[0])
    console.print(t)


def coarse_table(cands, top: int) -> None:
    t = Table(title=f"Quick estimate: top {min(top, len(cands))} of {len(cands)} ideas "
                    "(based on lowest market prices, not exact floats yet)", title_justify="left")
    for c in ("Collection", "Type", "Target", "Est. cost", "Est. return", "Est. price per input (cheapest that fits)", "Est. ROI"):
        t.add_column(c)
    for c in cands[:top]:
        t.add_row(c.collection.name,
                  ("StatTrak " if c.stattrak else "") + ("Covert -> Knife" if c.tier == COVERT else TIER_NAMES[c.tier]),
                  c.target_label, money(c.cost), money(c.value),
                  f"{c.size}x {money(c.input_price)} {c.input_desc}", pct(c.roi))
    console.print(t)


def doctor_table(checks) -> None:
    t = Table(title="Health check", title_justify="left")
    for c in ("Source", "Result", "Details"):
        t.add_column(c)
    marks = {"ok": "[green]OK[/]", "fail": "[bold red]FAIL[/]", "skip": "[dim]skipped[/]"}
    for c in checks:
        t.add_row(c.name, marks[c.status], c.detail)
    console.print(t)
    if any(c.status == "fail" for c in checks):
        console.print("[red]Something is broken.[/] A market that fails here will silently return no listings "
                      "in scans. Check your API key, or the market may have changed its API.")
    else:
        console.print("[green]Everything answers as expected.[/]")


def collection_table(col: Collection) -> None:
    console.rule(f"[bold]{col.name}[/]" + (f"  (case: {col.case_name})" if col.case_name else ""))
    for tier in sorted(col.tiers):
        t = Table(title=tier_label(tier), title_justify="left")
        for c in ("Skin", "Float range", "Conditions", "StatTrak", "Souvenir"):
            t.add_column(c)
        for s in {s.name: s for s in col.tiers[tier]}.values():  # one row per finish, not per phase
            t.add_row(s.name, f"{s.min_float:.2f} - {s.max_float:.2f}",
                      " ".join(possible_wears(s.min_float, s.max_float)),
                      "yes" if s.stattrak else "", "yes" if s.souvenir else "")
        console.print(t)
    if col.special_pool:
        console.print(f"[bold]Knives/gloves from 5 Coverts[/] ({len(col.special_pool)} possible): "
                      + ", ".join(col.special_pool))
