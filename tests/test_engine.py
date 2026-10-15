import pytest

from cs2tradeup.catalog import COVERT, Catalog, market_hash_name
from cs2tradeup.engine import Contract, ContractError, ContractInput, max_avg_for, normalize
from cs2tradeup.wear import f32, parse_target, possible_wears, wear_of


@pytest.fixture(scope="module")
def cat():
    return Catalog()


def inputs(skin, fl, n=10, **kw):
    return [ContractInput(skin, fl, **kw) for _ in range(n)]


def test_wear_boundaries():
    assert wear_of(0.0699999) == "FN"
    assert wear_of(0.07) == "MW"
    assert wear_of(0.15) == "FT"
    assert wear_of(0.38) == "WW"
    assert wear_of(0.45) == "BS"
    assert parse_target("MW", 1e-6) == pytest.approx(0.15 - 1e-6)
    assert parse_target("0.149999") == 0.149999
    assert possible_wears(0.06, 0.8) == ["FN", "MW", "FT", "WW", "BS"]
    assert possible_wears(0.0, 0.08) == ["FN", "MW"]
    assert possible_wears(0.1, 0.7) == ["MW", "FT", "WW", "BS"]


def test_redline_catalog(cat):
    s = cat.find_skin("AK-47 | Redline")
    assert (s.min_float, s.max_float) == (0.1, 0.7)
    assert s.def_index == 7 and s.paint_index == 282
    assert cat.collection_of(s).name == "The Phoenix Collection"
    assert market_hash_name(s, "FT", stattrak=True) == "StatTrak™ AK-47 | Redline (Field-Tested)"


def test_normalized_float(cat):
    s = cat.find_skin("AK-47 | Redline")      # 0.10 - 0.70
    assert normalize(s, 0.16) == pytest.approx(0.1)
    assert normalize(s, 0.16, "legacy") == 0.16


def test_single_collection_odds_and_float(cat):
    redline = cat.find_skin("AK-47 | Redline")   # Classified -> Phoenix Covert (AWP Asiimov only)
    c = Contract(cat, inputs(redline, 0.16))
    outs = c.outcomes()
    assert sum(o.probability for o in outs) == pytest.approx(1.0)
    col = cat.collection_of(redline)
    covert = col.tiers[5]
    assert len(outs) == len(covert)
    o = outs[0]
    # avg normalized 0.1 mapped onto the output range
    exp = o.skin.min_float + 0.1 * (o.skin.max_float - o.skin.min_float)
    assert o.float_value == pytest.approx(f32(exp))


def test_mixed_collection_odds(cat):
    redline = cat.find_skin("AK-47 | Redline")
    col_a = cat.collection_of(redline)
    other = next(c for c in cat.tradeup_collections(4) if c.id != col_a.id and len(c.tiers[5]) == 2)
    filler = other.tiers[4][0]
    ins = inputs(redline, 0.2, 3) + inputs(filler, filler.min_float, 7)
    outs = {o.name: o.probability for o in Contract(cat, ins).outcomes()}
    a_out = col_a.tiers[5]
    for s in a_out:
        assert outs[s.name] == pytest.approx(0.3 / len(a_out))
    for s in other.tiers[5]:
        assert outs[s.name] == pytest.approx(0.7 / 2)


def test_float_cap_differs_per_output(cat):
    """Same average lands differently on outputs with different ranges."""
    redline = cat.find_skin("AK-47 | Redline")
    ins = inputs(redline, 0.40)  # avg normalized 0.5
    for o in Contract(cat, ins).outcomes():
        lo, hi = o.skin.min_float, o.skin.max_float
        assert lo <= o.float_value <= hi + 1e-7
        assert o.float_value == pytest.approx(lo + 0.5 * (hi - lo), abs=1e-6)


def test_max_avg_for(cat):
    redline = cat.find_skin("AK-47 | Redline")  # 0.1-0.7
    assert max_avg_for(redline, 0.15) == pytest.approx(0.05 / 0.6)
    assert max_avg_for(redline, 0.05) < 0          # can never be FN
    assert max_avg_for(redline, 0.8) == 1.0


def test_validation(cat):
    redline = cat.find_skin("AK-47 | Redline")
    with pytest.raises(ContractError):
        Contract(cat, inputs(redline, 0.2, 9))
    with pytest.raises(ContractError):
        Contract(cat, inputs(redline, 0.05))       # below its min float
    with pytest.raises(ContractError):            # StatTrak + Souvenir mix
        Contract(cat, inputs(redline, 0.2, 10, souvenir=True), stattrak=True)


def test_souvenir_inputs_allowed(cat):
    souv = next(s for c in cat.collections.values() for s in c.tiers.get(2, [])
                if s.souvenir and c.outputs_for(2, False))
    c = Contract(cat, inputs(souv, souv.min_float, souvenir=True))
    assert c.outcomes()


def test_knife_contract(cat):
    col = cat.find_collection("Chroma Collection")
    cov = col.tiers[COVERT][0]
    c = Contract(cat, inputs(cov, cov.min_float, 5))
    outs = c.outcomes()
    assert sum(o.probability for o in outs) == pytest.approx(1.0)
    assert all(o.skin.is_special for o in outs)
    doppler = [o for o in outs if "Doppler" in o.name]
    assert doppler and len(doppler[0].variants) > 1          # phases grouped into one outcome
    with pytest.raises(ContractError):
        Contract(cat, inputs(cov, cov.min_float, 10))          # Covert needs exactly 5


def test_stattrak_knife_contract_excludes_gloves(cat):
    col = next(c for c in cat.collections.values()
               if c.tiers.get(COVERT) and any(g[0].is_glove for g in c.special_pool.values()))
    normal = col.outputs_for(COVERT, False)
    st = col.outputs_for(COVERT, True)
    assert any(g[0].is_glove for g in normal)
    assert not any(g[0].is_glove for g in st)


def test_phased_weapon_skin_is_one_outcome(cat):
    # 2021 Train: Covert outcomes are Glock-18 | Gamma Doppler (5 phases) and M4A4 | The Coalition
    col = cat.find_collection("2021 Train")
    pool = col.outputs_for(4, False)
    assert sorted(g[0].name for g in pool) == ["Glock-18 | Gamma Doppler", "M4A4 | The Coalition"]
    glock = next(g for g in pool if g[0].name.startswith("Glock"))
    assert len(glock) == 5
    skin = col.tiers[4][0]
    probs = {o.name: o.probability for o in Contract(cat, inputs(skin, skin.min_float)).outcomes()}
    assert probs["Glock-18 | Gamma Doppler"] == pytest.approx(0.5)
    assert probs["M4A4 | The Coalition"] == pytest.approx(0.5)


def test_search_ignores_missing_space(cat):
    kind, col = cat.search("chroma2", skins=False)[0]
    assert kind == "collection" and col.name == "The Chroma 2 Collection"
