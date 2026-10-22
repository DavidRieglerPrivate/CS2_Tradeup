"""Guided, menu-driven mode for people who don't want to type commands.

Start it by running `tradeup` with no arguments, or by double-clicking "Start Tradeup.bat".
"""
from __future__ import annotations

import webbrowser

from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
from rich.table import Table

from .actions import check_steam_id, markets_status, run_check, run_inventory, run_optimize, run_recheck, run_scan
from .catalog import TIER_NAMES, Catalog, Collection, Skin
from .config import load_config, save_keys
from .display import (collection_table, console, coverage_lines, inventory_table, print_result, results_overview,
                      tier_label)
from .engine import Contract, ContractError, ContractInput, contract_size
from .evaluate import Result
from .inventory import InventoryError
from .markets import OWNED
from .optimizer import Group
from .scanner import check_groups
from .wear import WEARS, parse_target


class Back(Exception):
    """User asked to go back to the main menu."""


# --------------------------------------------------------------------------- small prompt helpers
def choose(question: str, options: list[tuple[str, str]], default: int = 1) -> int:
    """Numbered menu. Returns the 1-based choice. Typing 0 or 'b' goes back."""
    console.print(f"\n[bold]{question}[/]")
    for i, (label, desc) in enumerate(options, 1):
        console.print(f"  [bold cyan]{i}[/]  {label}" + (f"  [dim]{desc}[/]" if desc else ""))
    console.print("  [dim]0  Back to main menu[/]")
    while True:
        raw = Prompt.ask("Your choice", default=str(default))
        if raw.strip().lower() in ("0", "b", "back"):
            raise Back
        if raw.strip().isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        console.print(f"[red]Please type a number from 1 to {len(options)}.[/]")


def ask_number(question: str, lo: int, hi: int, default: int | None = None) -> int:
    while True:
        v = IntPrompt.ask(question, default=default)
        if v is not None and lo <= v <= hi:
            return v
        console.print(f"[red]Please enter a number from {lo} to {hi}.[/]")


def ask_float(question: str, lo: float, hi: float) -> float:
    while True:
        try:
            v = FloatPrompt.ask(question)
        except Exception:
            v = None
        if v is not None and lo <= v <= hi:
            return v
        console.print(f"[red]Please enter a number between {lo} and {hi}, for example {lo + (hi - lo) / 4:.4f}.[/]")


def ask_count(left: int) -> int | None:
    """How many of one item: a number from 1 to `left`, or None (Enter) to let the program decide."""
    while True:
        raw = Prompt.ask(f"How many of these? (1-{left}, or press Enter to let the program decide)",
                         default="", show_default=False).strip()
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= left:
            return int(raw)
        console.print(f"[red]Please type a number from 1 to {left}, or just press Enter.[/]")


def ask_money(question: str) -> float:
    while True:
        raw = Prompt.ask(question, default="", show_default=False).strip().replace("$", "").replace(",", ".")
        if not raw:
            return 0.0
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
        console.print("[red]Please type an amount like 50 or 120.5, or just press Enter for no limit.[/]")


def why_not_found(cat: Catalog, q: str, tier: int | None, skins: bool, collections: bool, no_st: list) -> str:
    """Explain an empty search: usually the rarity or StatTrak™ filter hid what the user typed."""
    if no_st:
        return f"{no_st[0][1].name} has no StatTrak™ version, so it can't go into a StatTrak™ trade-up."
    if tier is not None:
        other = cat.search(q, skins=skins, collections=collections)
        if other:
            kind, obj = other[0]
            if kind == "skin":
                return (f"{obj.name} is {TIER_NAMES[obj.tier]}, but this trade-up is {TIER_NAMES[tier]}: all "
                        "skins in a trade-up must have the same rarity.")
            return f"{obj.name} has no {TIER_NAMES[tier]} skins."
    if skins and not collections and cat.search(q, skins=False):
        return "That's a collection. Type the name of one skin, e.g. 'AK-47 | Redline'."
    return "Nothing found. Try part of the name, e.g. 'redline' or 'asiimov'."


def pick_from_search(cat: Catalog, prompt: str, tier: int | None = None, skins: bool = True,
                     collections: bool = True, stattrak: bool = False) -> tuple[str, object] | None:
    """Ask for a name, show matches, let the user pick one. Returns None when the user presses Enter."""
    q = ""
    while True:
        if not q:
            q = Prompt.ask(prompt + " [dim](0 = main menu)[/]", default="", show_default=False).strip()
        if not q:
            return None
        if q.lower() in ("0", "back"):
            raise Back
        hits = cat.search(q, tier=tier, skins=skins, collections=collections)
        no_st = [h for h in hits if h[0] == "skin" and not h[1].stattrak] if stattrak else []
        hits = [h for h in hits if h not in no_st]
        if not hits:
            console.print(f"[yellow]{why_not_found(cat, q, tier, skins, collections, no_st)}[/]")
            q = ""
            continue
        t = Table(show_header=True)
        t.add_column("#", justify="right")
        t.add_column("Name")
        t.add_column("Type")
        t.add_column("Details", style="dim")
        for i, (kind, obj) in enumerate(hits, 1):
            if kind == "skin":
                s: Skin = obj
                col = cat.collection_of(s)
                t.add_row(str(i), s.name, tier_label(s.tier),
                          f"float {s.min_float:.2f}-{s.max_float:.2f}, {col.name if col else ''}")
            else:
                c: Collection = obj
                t.add_row(str(i), c.name, "collection", c.case_name or "")
        console.print(t)
        if len(hits) == 1:
            ask = "Press Enter to use it, or type another name to search again"
        else:
            ask = f"Type its number from the # column (1-{len(hits)}), or another name to search again"
        while True:
            raw = Prompt.ask(ask, default="1" if len(hits) == 1 else "", show_default=False).strip()
            if raw.lower() in ("0", "back"):
                raise Back
            if raw.isdigit():
                if 1 <= int(raw) <= len(hits):
                    return hits[int(raw) - 1]
                # usually a quantity typed too early ("5" for five Coverts)
                console.print(f"[yellow]{raw} isn't in the # column. Pick the row first"
                              + (" (just press Enter)" if len(hits) == 1 else f" (1-{len(hits)})")
                              + "; you'll be asked how many you want right after.[/]")
                continue
            q = raw   # a new name: search again straight away (empty = back to the name prompt)
            break


RARITY_OPTIONS = [
    (0, "Consumer -> Industrial", "cheapest, very low profits"),
    (1, "Industrial -> Mil-Spec", ""),
    (2, "Mil-Spec -> Restricted", "popular, cheap to try"),
    (3, "Restricted -> Classified", ""),
    (4, "Classified -> Covert", "expensive, bigger results"),
    (5, "Covert -> Knife / Gloves", "5 skins in, knife or gloves out"),
]


def ask_condition(allow_any: bool) -> list[tuple[str, float]]:
    margin = load_config().safety_margin
    opts = [
        ("Factory New (FN)", "best condition, float below 0.07"),
        ("Minimal Wear (MW)", "float below 0.15"),
        ("Field-Tested (FT)", "float below 0.38, cheapest to hit"),
        ("Well-Worn (WW)", "float below 0.45"),
        ("Exact maximum float", "e.g. 0.149999"),
    ]
    if allow_any:
        opts.insert(0, ("Try FN, MW and FT", "recommended, finds the best of all three"))
    i = choose("What condition should the skin you get back have? (lower float = better = usually pricier)",
               opts, default=1)
    if allow_any:
        if i == 1:
            return [(c, parse_target(c, margin)) for c in ("FN", "MW", "FT")]
        i -= 1
    if i == 5:
        v = ask_float("Maximum float for the result (between 0.001 and 1)", 0.001, 1.0)
        return [(f"<={v}", v)]
    code = WEARS[i - 1][0]
    return [(code, parse_target(code, margin))]


STEAM_HELP = ("Paste the link to your Steam profile (steamcommunity.com/id/... or /profiles/...). Your inventory "
              "must be public: Steam > Profile > Edit Profile > Privacy Settings > Inventory: Public. "
              "Skins inside storage units can't be read; take them out first.")


def ask_steam_profile() -> bool:
    """Ask for the Steam profile, check its inventory can be read and save it. False if none was set."""
    console.print(f"[dim]{STEAM_HELP}[/]")
    raw = Prompt.ask("Steam profile link (press Enter to skip)", default="", show_default=False).strip()
    if not raw:
        return False
    try:
        with console.status("Checking that your Steam inventory can be read..."):
            sid = check_steam_id(load_config(), raw)
    except InventoryError as e:
        console.print(f"[red]Can't use that profile: {e}[/]")
        return False
    save_keys({"steam_id": sid})
    console.print(f"[green]Saved your Steam profile ({sid}).[/]")
    return True


def ask_use_inventory() -> bool:
    """Whether to include skins the user already owns. Asks for the Steam profile the first time."""
    if load_config().key("steam_id"):
        return Confirm.ask("Also use skins you already own (from your Steam inventory)?", default=False)
    if not Confirm.ask("Also use skins you already own? This needs your Steam profile", default=False):
        return False
    return ask_steam_profile()


def parse_numbers(raw: str, hi: int) -> list[int] | None:
    """'1-3, 5 7' -> [1, 2, 3, 5, 7]; None when something isn't a number from 1 to hi."""
    out = []
    for part in raw.replace(",", " ").split():
        a, _, b = part.partition("-")
        if not a.isdigit() or (b and not b.isdigit()):
            return None
        lo, up = int(a), int(b or a)
        if not 1 <= lo <= up <= hi:
            return None
        out += range(lo, up + 1)
    return list(dict.fromkeys(out)) or None


def pick_owned(cat: Catalog) -> tuple[list[ContractInput], bool] | None:
    """Let the user pick a full contract from their inventory: (inputs, StatTrak). None = they went back."""
    if not load_config().key("steam_id") and not ask_steam_profile():
        return None
    owned, total = run_inventory(load_config(), cat)
    if not owned:
        console.print(f"[yellow]None of the {total} items in your inventory can go into a trade-up (only weapon "
                      "skins with a higher rarity in their collection can).[/]")
        return None
    inventory_table(cat, owned)
    console.print("[dim]Worth = what the skin would sell for after the sell fee. All picked skins must have the "
                  "same rarity, and StatTrak™ can't mix with normal ones.[/]")
    while True:
        raw = Prompt.ask("Type the numbers of the skins to use, e.g. 1-10 or 1,3,5 (Enter = back)",
                         default="", show_default=False).strip()
        if not raw:
            return None
        idx = parse_numbers(raw, len(owned))
        if idx is None:
            console.print(f"[red]Use numbers from the # column (1-{len(owned)}), like 1-10 or 2,4,6.[/]")
            continue
        chosen = [owned[i - 1] for i in idx]
        if len({(l.skin.tier, l.stattrak) for l in chosen}) > 1:
            console.print("[red]Those skins differ in rarity or StatTrak™. Pick skins of one kind only.[/]")
            continue
        n = contract_size(chosen[0].skin.tier)
        if len(chosen) != n:
            console.print(f"[red]This rarity needs exactly {n} skins, you picked {len(chosen)}.[/]"
                          + (" To fill the rest with the cheapest market listings, use 'Build my own trade-up' "
                             "and say yes to using your own skins." if len(chosen) < n else ""))
            continue
        return ([ContractInput(l.skin, l.float_value, souvenir=l.souvenir, price=l.cost, source=OWNED)
                 for l in chosen], chosen[0].stattrak)


def offer_links(res: Result) -> None:
    links = [i.link for i in res.contract.inputs if i.link]
    if links and Confirm.ask(f"Open the {len(links)} listing pages in your web browser?", default=False):
        for link in dict.fromkeys(links):
            webbrowser.open_new_tab(link)


def browse_results(results: list[Result], cfg=None, cat: Catalog | None = None) -> None:
    while True:
        results_overview(results)
        raw = Prompt.ask("Type a number to see details and buy links, or press Enter to go back",
                         default="", show_default=False).strip()
        if not raw:
            return
        if raw.isdigit() and 1 <= int(raw) <= len(results):
            res = results[int(raw) - 1]
            if not res.confirmed and cfg is not None:
                # listings can be up to listing_cache_minutes old: make sure they're still for sale first
                new, sold = run_recheck(cfg, cat, res)
                if new is None:
                    console.print(f"[yellow]{sold} of these listings sold, and no replacement reaches the same "
                                  "condition. This trade-up is gone, pick another one.[/]")
                    results.pop(int(raw) - 1)
                    if not results:
                        return
                    continue
                if sold:
                    console.print(f"[dim]{sold} listing(s) had sold or changed price and were replaced.[/]")
                res = results[int(raw) - 1] = new
            print_result(res, int(raw))
            offer_links(res)
            Prompt.ask("Press Enter to return to the list", default="", show_default=False)
        else:
            console.print(f"[red]Please type a number from 1 to {len(results)}.[/]")


# --------------------------------------------------------------------------- menu actions
def action_scan(cat: Catalog) -> None:
    cfg = load_config()
    connected = [k for k, v in markets_status(cfg).items() if v]
    console.print(Panel(
        "The program estimates every possible trade-up from current market prices, then checks real listings "
        "and their floats for all of them, most promising first, and picks the exact skins to buy. Markets "
        "limit how fast we can ask, so you choose how long it may take.",
        title="Find profitable trade-ups", border_style="cyan"))
    if not connected:
        console.print("[yellow]No market API keys are set, so you'll only get a quick estimate without buy "
                      "links. Use menu option 5 to add keys.[/]")

    opts = [("All gun rarities", "Consumer up to Classified")] + [(l, d) for _, l, d in RARITY_OPTIONS]
    i = choose("Which trade-up do you want to look for?", opts, default=1)
    tiers = [0, 1, 2, 3, 4] if i == 1 else [RARITY_OPTIONS[i - 2][0]]
    targets = ask_condition(allow_any=True)
    m = choose("StatTrak™?", [("Normal only", ""), ("StatTrak™ only", ""), ("Both", "takes longer")], default=3)
    modes = {1: [False], 2: [True], 3: [False, True]}[m]
    use_inventory = bool(connected) and ask_use_inventory()
    budget = ask_money("Maximum you want to spend on one trade-up in USD"
                       + (", not counting your own skins" if use_inventory else "") + " (press Enter for no limit)")
    depth = choose("How long may the search take? Ideas are checked most promising first; a scan that runs "
                   "out of time says how many it covered.", [
        ("Quick", "up to 2 minutes"),
        ("Normal", "up to 5 minutes"),
        ("Thorough", "up to 15 minutes"),
        ("Complete", "every idea on every market; can take an hour or more"),
    ], default=2)
    time_limit = {1: 2, 2: 5, 3: 15, 4: None}[depth]
    only_profit = Confirm.ask("Only show trade-ups that make money on average?", default=True)

    if budget:
        cfg["scan"]["max_contract_cost"] = budget
    out = run_scan(cfg, cat, tiers, modes, targets, souvenir=True, top=None, time_limit=time_limit,
                   min_roi=0.0 if only_profit else -1.0, coarse_only=not connected, show_coarse=not connected,
                   use_inventory=use_inventory)
    if not out.candidates:
        console.print("[yellow]No ideas matched those settings. Try a higher budget or another rarity.[/]")
        return
    if not out.deep_ran:
        console.print("[dim]These are estimates only. Add API keys (option 5) to check real listings.[/]")
        return
    if out.owned is not None:
        console.print(f"[dim]{out.owned} of your own skins fit these settings; the ideas they can go into were "
                      "checked first.[/]" if out.owned else
                      "[yellow]None of your own skins fit these settings (rarity, StatTrak™), so only market "
                      "listings were used.[/]")
    for line in coverage_lines(out.coverage):
        console.print(line)
    if out.errors:
        console.print(f"[yellow]{len(out.errors)} market request(s) failed, results may be incomplete.[/]")
    if out.sold:
        console.print(f"[dim]{out.sold} chosen listing(s) sold during the scan and were replaced.[/]")
    if not out.results:
        console.print("[yellow]None of the checked trade-ups make money with the listings available right "
                      "now. Try again later, another rarity, or untick 'only profitable' to see the closest "
                      "ones.[/]")
        return
    console.print(f"[dim]The best {out.confirmed} were re-checked as still for sale; any other one is "
                  "re-checked when you open it.[/]")
    browse_results(out.results, cfg, cat)


def action_build(cat: Catalog) -> None:
    console.print(Panel(
        "Pick the skins or collections you want to use. The program searches all markets for the cheapest "
        "listings that still give you the condition you want, and shows links to buy them.\n"
        "Tip: you can add a whole collection, and the program picks the best skins from it.",
        title="Build your own trade-up", border_style="cyan"))
    groups: list[Group] = []
    tier: int | None = None
    stattrak = False
    fixed = 0
    flexible = False
    while True:
        if tier is not None:
            n = contract_size(tier)
            console.print(f"\n[bold]Slots used: {fixed} of {n}[/]" + (" (plus 'program decides' items)" if flexible
                                                                        else ""))
            if fixed >= n:
                break
        hit = pick_from_search(cat, "Type a skin or collection name" + (" (press Enter when done)"
                                                                         if groups else ""),
                               tier=tier, stattrak=stattrak)
        if hit is None:
            if not groups:
                continue
            break
        kind, obj = hit
        new_tier = tier
        if kind == "collection" and tier is None:
            avail = [t for t, _, _ in RARITY_OPTIONS if obj.tiers.get(t) and obj.outputs_for(t, False)]
            if not avail:
                console.print("[red]Nothing in this collection can be traded up.[/]")
                continue
            i = choose("Which rarity from this collection?",
                       [(TIER_NAMES[t], f"{len({s.name for s in obj.tiers[t]})} skins") for t in avail])
            new_tier = avail[i - 1]
        elif kind == "skin" and tier is None:
            new_tier = obj.tier
        col = obj if kind == "collection" else cat.collection_of(obj)
        if not col or not col.outputs_for(new_tier, stattrak):
            console.print(f"[red]{obj.name} can't be traded up: {TIER_NAMES[new_tier]} is the highest rarity in "
                          f"{col.name if col else 'its collection'}.[/]")
            continue
        ids = {obj.id} if kind == "skin" else {s.id for s in obj.tiers.get(new_tier, [])}
        if tier is None:
            # asked now, not at the end: it decides which skins can be added from here on
            tier = new_tier
            if any(cat.skins[sid].stattrak for sid in ids):
                stattrak = Confirm.ask("StatTrak™ trade-up? (StatTrak™ and normal skins can't be mixed)",
                                       default=False)
        if stattrak:
            ids = {sid for sid in ids if cat.skins[sid].stattrak}
            if not ids:
                console.print(f"[red]{obj.name} has no StatTrak™ {TIER_NAMES[tier]} skins.[/]")
                continue
        if any(g.skin_ids == ids for g in groups):
            console.print(f"[yellow]{obj.name} is already in this trade-up.[/]")
            continue
        n = contract_size(tier)
        count = ask_count(n - fixed)
        if count is None:
            lo, hi, flexible = 0, n, True
        else:
            lo = hi = count
        groups.append(Group(ids, lo, hi, obj.name))
        fixed += lo
    n = contract_size(tier)
    if fixed < n and not flexible:
        console.print(f"[dim]You picked {fixed} of {n}. The remaining slots are filled with the items you chose.[/]")
        for g in groups:
            g.max_count = n
    try:
        check_groups(cat, groups, tier, stattrak)
    except ValueError as e:
        console.print(f"[red]This trade-up isn't possible: {e}[/]")
        return

    use_inventory = ask_use_inventory()
    targets = ask_condition(allow_any=False)
    _, max_out = targets[0]
    goal = 1
    if flexible:   # with every count fixed, the cheapest skins are also the most profitable ones
        goal = choose("What matters most?", [
            ("Cheapest skins that reach the condition", "lowest total price"),
            ("Highest expected profit", "may use more of the collections whose results are worth more"),
        ], default=1)
    res, msg = run_optimize(load_config(), cat, groups, tier, stattrak, max_out, souvenir=not stattrak,
                            objective="cost" if goal == 1 else "profit", use_inventory=use_inventory)
    if not res:
        console.print(f"[yellow]{msg}[/]")
        return
    console.print(f"[dim]{msg}[/]")
    print_result(res)
    offer_links(res)


def action_check(cat: Catalog) -> None:
    console.print(Panel(
        "Enter the skins you already have (or are thinking of buying) together with their float values. "
        "The program shows what you can get, with what chance, and whether it's worth it.\n"
        "You can find a skin's float by inspecting it in game or on any market listing.",
        title="Check a trade-up", border_style="cyan"))
    source = choose("Where are the skins?", [
        ("In my Steam inventory", "pick them from a list, floats are read for you"),
        ("I'll type them in", "names and floats by hand"),
    ], default=1)
    if source == 1:
        picked = pick_owned(cat)
        if picked is None:
            return
        inputs, stattrak = picked
        try:
            contract = Contract(cat, inputs, stattrak=stattrak, formula=load_config().float_formula)
        except ContractError as e:
            console.print(f"[red]This trade-up isn't possible: {e}[/]")
            return
        res = run_check(load_config(), contract)
        print_result(res)
        return
    inputs: list[ContractInput] = []
    tier = None
    stattrak = False
    while True:
        n = contract_size(tier) if tier is not None else 10
        if inputs:
            console.print(f"\n[bold]{len(inputs)} of {n} skins added[/]")
        if inputs and len(inputs) >= n:
            break
        hit = pick_from_search(cat, "Type the name of a skin", tier=tier, collections=False, stattrak=stattrak)
        if hit is None:
            if inputs:
                console.print(f"[dim]A {TIER_NAMES[tier]} trade-up needs exactly {n} skins, {n - len(inputs)} "
                              "more to go (0 = main menu).[/]")
            continue
        s: Skin = hit[1]
        col = cat.collection_of(s)
        if not col or not col.outputs_for(s.tier, stattrak):
            console.print("[red]This skin is the highest rarity in its collection, so it can't be traded up.[/]")
            continue
        if tier is None:
            tier = s.tier
            n = contract_size(tier)
            stattrak = s.stattrak and Confirm.ask("Is it StatTrak™? (then all of them must be StatTrak™)",
                                                  default=False)
        fl = ask_float(f"Float of this {'StatTrak™ ' if stattrak else ''}{s.name} "
                       f"(allowed {s.min_float:.2f} to {s.max_float:.2f})", s.min_float, s.max_float)
        count = ask_number(f"How many with this float? (1-{n - len(inputs)})", 1, n - len(inputs), default=1)
        souv = not stattrak and s.souvenir and Confirm.ask("Is it a Souvenir?", default=False)
        inputs += [ContractInput(s, fl, souvenir=souv) for _ in range(count)]
    try:
        contract = Contract(cat, inputs, stattrak=stattrak, formula=load_config().float_formula)
    except ContractError as e:
        console.print(f"[red]This trade-up isn't possible: {e}[/]")
        return
    res = run_check(load_config(), contract)
    console.print("[dim]Your skins are valued at the cheapest current listing of their condition.[/]")
    print_result(res)


def action_collection(cat: Catalog) -> None:
    hit = pick_from_search(cat, "Type a collection or case name", skins=False)
    if hit:
        collection_table(hit[1])


def action_keys() -> None:
    cfg = load_config()
    st = markets_status(cfg)
    console.print(Panel(
        "API keys let the program read listings with floats. They're stored only on this computer, in "
        "config.toml.\n"
        "* CSFloat: csfloat.com > Profile > Developers\n"
        "* DMarket: dmarket.com > Settings > Trading API (public + secret key)\n"
        "* Waxpeer: waxpeer.com > Profile > Security > API key\n"
        "Press Enter at any question to keep the current key.",
        title="Market API keys", border_style="cyan"))
    for name, ok in st.items():
        console.print(f"  {name}: " + ("[green]connected[/]" if ok else "[red]not set[/]"))
    new = {}
    for key, label in (("csfloat", "CSFloat API key"), ("waxpeer", "Waxpeer API key"),
                       ("dmarket_public", "DMarket public key"), ("dmarket_secret", "DMarket secret key")):
        v = Prompt.ask(label, password=True, default="", show_default=False).strip()
        if v:
            new[key] = v
    if new:
        path = save_keys(new)
        console.print(f"[green]Saved {len(new)} key(s) to {path.name}.[/]")
    else:
        console.print("Nothing changed.")
    sid = load_config().key("steam_id")
    console.print("\n[bold]Steam profile[/] (to use skins you already own): "
                  + (f"[green]{sid}[/]" if sid else "[red]not set[/]"))
    if not sid or Confirm.ask("Change it?", default=False):
        ask_steam_profile()


def action_doctor(cat: Catalog) -> None:
    from .actions import run_doctor
    from .display import doctor_table

    with console.status("Asking every market for a few listings..."):
        checks = run_doctor(load_config(), cat)
    doctor_table(checks)


HELP = """\
[bold]What is a trade-up?[/]
You give the game 10 skins of the same rarity and get 1 skin of the next rarity back.
5 Covert skins give you a knife or gloves. StatTrak™ skins can only be mixed with StatTrak™ skins.

[bold]What can I get?[/]
Only skins from the same collections as the skins you put in. The more of your 10 skins come from a
collection, the higher the chance the result comes from that collection.

[bold]What decides the condition (float) of the result?[/]
Each skin has a float between 0 and 1 (lower = better). The program works out how "good" each of your
skins is within its own possible range, averages that, and applies it to the range of the result.
So cheap low-float skins can make a Factory New or Minimal Wear result.

[bold]How does the program value things?[/]
Results are valued at their recent median sale price on Skinport, minus the fee for selling
(2% on CSFloat by default). Skins to buy use the real listing prices on CSFloat, DMarket and Waxpeer.

[bold]Is it safe money?[/]
No. A trade-up is a gamble: "average profit" means over many attempts. Check the "chance of profit"
and the worst case before buying. Prices change quickly.
"""


def run() -> None:
    with console.status("Loading the skin database (the first start downloads it)..."):
        cat = Catalog()
    actions = [
        ("Find profitable trade-ups for me", "automatic scan", lambda: action_scan(cat)),
        ("Build my own trade-up", "pick skins, get the cheapest buy links", lambda: action_build(cat)),
        ("Check a trade-up with skins I have", "from your Steam inventory or typed in", lambda: action_check(cat)),
        ("Look at a collection", "skins, rarities, float ranges", lambda: action_collection(cat)),
        ("Set up market API keys", "and your Steam profile", action_keys),
        ("How do trade-ups work?", "", lambda: console.print(Panel(HELP, border_style="cyan"))),
        ("Check that the markets are working", "about a minute", lambda: action_doctor(cat)),
    ]
    while True:
        st = markets_status(load_config())
        status = "   ".join(f"{k} " + ("[green]connected[/]" if v else "[red]not set[/]") for k, v in st.items())
        console.print()
        console.print(Panel(f"[bold]CS2 Trade-Up Helper[/]\nMarkets: {status}", border_style="bright_blue"))
        for i, (label, desc, _) in enumerate(actions, 1):
            console.print(f"  [bold cyan]{i}[/]  {label}" + (f"  [dim]{desc}[/]" if desc else ""))
        console.print("  [bold cyan]0[/]  Exit")
        # no default: a stray Enter must not start a scan that runs for minutes
        raw = Prompt.ask("What do you want to do?", default="", show_default=False).strip()
        if not raw:
            continue
        if raw in ("0", "q", "exit", "quit"):
            console.print("Bye!")
            return
        if not (raw.isdigit() and 1 <= int(raw) <= len(actions)):
            console.print(f"[red]Please type a number from 0 to {len(actions)}.[/]")
            continue
        try:
            actions[int(raw) - 1][2]()
        except Back:
            continue
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped. Back to the main menu.[/]")
        except Exception as e:  # never dump a traceback on a non-technical user
            console.print(f"[red]Something went wrong:[/] {e}\n"
                          "[dim]If this keeps happening, check your internet connection and API keys.[/]")
        Prompt.ask("\nPress Enter to return to the main menu", default="", show_default=False)
