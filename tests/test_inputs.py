"""User input handling: names, counts, conditions, the build/check menus and how inputs are listed."""
import builtins

import pytest
from typer.testing import CliRunner

from cs2tradeup import interactive
from cs2tradeup.catalog import Catalog, split_item_name
from cs2tradeup.cli import app
from cs2tradeup.markets import OWNED, Listing
from cs2tradeup.optimizer import Group, Problem, solve
from cs2tradeup.scanner import check_groups, contract_groups
from cs2tradeup.wear import parse_target
from tests.test_optimizer import FlatPrices


@pytest.fixture(scope="module")
def cat():
    return Catalog()


@pytest.mark.parametrize("text, expect", [
    ("AK-47 | Redline", ("AK-47 | Redline", False, False, None)),
    ("StatTrak™ AK-47 | Redline (Field-Tested)", ("AK-47 | Redline", True, False, "Field-Tested")),
    ("Souvenir AWP | Dragon Lore (Factory New)", ("AWP | Dragon Lore", False, True, "Factory New")),
    ("souvenir:MAC-10 | Gold Brick", ("MAC-10 | Gold Brick", False, True, None)),
    ("ST AK-47 | Redline", ("AK-47 | Redline", True, False, None)),
    ("★ Karambit | Doppler", ("Karambit | Doppler", False, False, None)),
    ("M4A4 | 龍王 (Dragon King) (Minimal Wear)", ("M4A4 | 龍王 (Dragon King)", False, False, "Minimal Wear")),
])
def test_split_item_name(text, expect):
    assert split_item_name(text) == expect


@pytest.mark.parametrize("query, name", [
    ("ak redline", "AK-47 | Redline"), ("ak47 redlin", "AK-47 | Redline"),
    ("StatTrak™ AK-47 | Redline (Field-Tested)", "AK-47 | Redline"), ("awp | boom", "AWP | BOOM"),
    ("M4A4 | 龍王 (Dragon King)", "M4A4 | 龍王 (Dragon King)"),
])
def test_find_skin_forgives(cat, query, name):
    assert cat.find_skin(query).name == name


@pytest.mark.parametrize("query, msg", [("Redline", "ambiguous"), ("★ Karambit | Doppler", "knife or gloves"),
                                        ("karambit doppler", "knife or gloves"), ("zzzz qqqq", "no skin named")])
def test_find_skin_explains(cat, query, msg):
    with pytest.raises(KeyError, match=msg):
        cat.find_skin(query)


def test_find_collection_typos_and_unknown(cat):
    assert cat.find_collection("phonix").name == "The Phoenix Collection"
    with pytest.raises(KeyError, match="no collection or case named"):
        cat.find_collection("xyz")


def test_souvenir_only_where_a_souvenir_package_has_it(cat):
    # the dataset's own flag says yes for nearly everything, including skins that never had a Souvenir
    assert not cat.find_skin("AK-47 | Redline").souvenir
    assert not cat.find_skin("AWP | Asiimov").souvenir
    assert cat.find_skin("AWP | Dragon Lore").souvenir and cat.find_skin("MAC-10 | Gold Brick").souvenir


@pytest.mark.parametrize("args, out", [
    (["-i", "ak redline @ 0,16 x 10"], "Average input value: 0.100000"),
    (["-i", "StatTrak™ AK-47 | Redline (Field-Tested)@0.16x10"], "Average input value"),
    (["-i", "StatTrak™ AK-47 | Redline@0.16x5", "-i", "AK-47 | Redline@0.16x5"], "can't be mixed"),
    (["-i", "Souvenir MAC-10 | Gold Brick@0.2x10"], "Average input value"),
    (["-i", "souvenir:AK-47 | Redline@0.16x10"], "has no Souvenir version"),
    (["-i", "AK-47 | Redline@abc"], "can't parse"),
])
def test_calc_inputs(args, out):
    res = CliRunner().invoke(app, ["calc", "--no-prices", *args])
    assert out in res.output


@pytest.mark.parametrize("specs, tier, msg", [
    (["AK-47 | Redline=abc"], None, "can't read the count"),
    (["AK-47 | Redline=5-2"], None, "must not be bigger"),
    (["AWP | Asiimov=5"], 4, "is Covert, not Classified"),
    (["AWP | Dragon Lore=5"], None, "highest rarity"),
    (["col:Cobblestone=5"], 5, "highest rarity"),
    (["col:Phoenix=10"], None, "need --tier"),
])
def test_contract_group_errors(cat, specs, tier, msg):
    with pytest.raises(ValueError, match=msg):
        contract_groups(cat, specs, tier)


@pytest.mark.parametrize("specs, tier, stattrak, msg", [
    (["AK-47 | Redline=3"], None, False, "at most 3 skins"),
    (["AK-47 | Redline=11"], None, False, "only 10 slots"),
    (["AK-47 | Redline=6", "AWP | Redline=6"], None, False, "more than the 10 slots"),
    (["MAC-10 | Gold Brick=10"], None, True, "no StatTrak"),
    (["col:Phoenix=10"], 5, False, "only 5 slots"),
])
def test_impossible_counts_are_explained(cat, specs, tier, stattrak, msg):
    groups, t = contract_groups(cat, specs, tier)
    with pytest.raises(ValueError, match=msg):
        check_groups(cat, groups, t, stattrak)


def test_possible_counts_pass(cat):
    for specs in (["AK-47 | Redline=3", "col:Phoenix=7"], ["AK-47 | Redline"], ["AK-47 | Redline=2-5", "AWP | Redline"]):
        groups, t = contract_groups(cat, specs)
        check_groups(cat, groups, t, False)


@pytest.mark.parametrize("text, value", [("fn", 0.07 - 1e-6), ("Field-Tested", 0.38 - 1e-6),
                                         ("field tested", 0.38 - 1e-6), ("0,15", 0.15), ("BS", 1.0)])
def test_parse_target(text, value):
    assert parse_target(text, 1e-6) == pytest.approx(value)


def test_parse_target_error():
    with pytest.raises(ValueError, match="unknown condition"):
        parse_target("zz")


def _listings(skin, n, price, market="shop", fl=0.2, prefix="l"):
    return [Listing(market, f"{prefix}{skin.id}{i}", skin, skin.name, fl, price + i * 0.01, price + i * 0.01,
                    False, False, "x") for i in range(n)]


def test_a_skin_in_two_groups_counts_for_one_of_them(cat):
    # "3 Redlines + 7 from Phoenix": Redline is in both groups, which used to make the first one impossible
    red = cat.find_skin("AK-47 | Redline")
    nova = cat.find_skin("Nova | Antique")
    ls = _listings(red, 12, 1.0) + _listings(nova, 12, 5.0)
    groups, tier = contract_groups(cat, ["AK-47 | Redline=3", "col:Phoenix=7"])
    res = solve(cat, FlatPrices(), Problem(tier, False, 0.38, ls, groups=groups, objective="cost"))
    assert res is not None
    # the cheap Redlines fill both groups: 3 as themselves, 7 more as "from Phoenix"
    assert sum(l.skin.id == red.id for l in res.listings) == 10


def test_inputs_are_listed_yours_first_then_per_skin_cheapest_first(cat):
    red, nova, trig = (cat.find_skin(n) for n in ("AK-47 | Redline", "Nova | Antique", "P90 | Trigon"))
    ls = (_listings(trig, 6, 1.0) + _listings(red, 4, 0.5, market="dm", prefix="a")
          + [Listing(OWNED, "own1", nova, nova.name, 0.2, 2.0, 2.0, False, False, "")])
    groups = [Group({red.id, nova.id, trig.id}, 9, 9, "Phoenix"), Group({nova.id}, 1, 1, "Nova")]
    res = solve(cat, FlatPrices(), Problem(red.tier, False, 0.38, ls, groups=groups, objective="cost"))
    names = [l.skin.name for l in res.listings]
    assert res.listings[0].market == OWNED
    assert names[1:] == sorted(names[1:])                       # one block per skin
    for skin in (red, trig):
        prices = [l.cost for l in res.listings if l.skin.id == skin.id]
        assert prices == sorted(prices)                         # cheapest first within a block


def run_menu(monkeypatch, fn, cat, answers):
    feed = iter(answers)
    calls = {}

    def fake_input(prompt=""):
        try:
            return next(feed)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(interactive, "ask_use_inventory", lambda: False)
    monkeypatch.setattr(interactive, "run_optimize",
                        lambda *a, **kw: calls.update(args=a, kw=kw) or (None, "stub"))
    monkeypatch.setattr(interactive, "run_check", lambda cfg, contract: calls.update(contract=contract) or (_ for _ in ()).throw(EOFError))
    try:
        fn(cat)
    except EOFError:
        pass
    return calls


def test_build_asks_stattrak_right_after_the_first_skin(cat, monkeypatch):
    # StatTrak yes, then a skin without StatTrak is refused at the search, then a fitting one is added
    calls = run_menu(monkeypatch, interactive.action_build, cat,
                     ["ak-47 | redline", "", "y", "4", "awp | redline", "", "6", "3"])
    groups, tier, stattrak = calls["args"][2], calls["args"][3], calls["args"][4]
    assert stattrak is True and tier == 4
    assert [(g.label, g.min_count) for g in groups] == [("AK-47 | Redline", 4), ("AWP | Redline", 6)]


def test_build_re_asks_bad_counts_and_skips_duplicates(cat, monkeypatch):
    calls = run_menu(monkeypatch, interactive.action_build, cat,
                     ["ak-47 | redline", "", "n", "12", "abc", "3", "ak-47 | redline", "", "awp | redline", "", "7",
                      "3"])
    groups = calls["args"][2]
    assert [(g.label, g.min_count, g.max_count) for g in groups] == [("AK-47 | Redline", 3, 3),
                                                                      ("AWP | Redline", 7, 7)]
    assert calls["kw"]["objective"] == "cost"   # every count fixed: no "what matters most" question


def test_check_asks_stattrak_first_and_no_souvenir_question_for_case_skins(cat, monkeypatch):
    calls = run_menu(monkeypatch, interactive.action_check, cat, ["2", "ak redline", "", "y", "0.2", "10"])
    c = calls["contract"]
    assert c.stattrak and len(c.inputs) == 10 and not any(i.souvenir for i in c.inputs)


@pytest.mark.parametrize("query, tier, collections, msg", [
    ("mac-10 gold brick", 4, True, "is Restricted, but this trade-up is Classified"),
    ("phoenix", None, False, "That's a collection"),
    ("zzqq", None, True, "Nothing found"),
])
def test_why_a_search_finds_nothing(cat, query, tier, collections, msg):
    assert msg in interactive.why_not_found(cat, query, tier, True, collections, [])
