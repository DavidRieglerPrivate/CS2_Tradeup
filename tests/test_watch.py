import pytest

from cs2tradeup import watch as w
from cs2tradeup.actions import ScanOutcome
from cs2tradeup.catalog import Catalog
from cs2tradeup.config import load_config
from cs2tradeup.engine import Contract, ContractInput
from cs2tradeup.evaluate import evaluate
from cs2tradeup.markets import Listing
from tests.test_optimizer import FlatPrices


@pytest.fixture(scope="module")
def cat():
    return Catalog()


def make_result(cat, ids):
    s = cat.find_skin("AK-47 | Redline")
    ls = [Listing("csfloat", i, s, s.name, 0.2, 5.0, 5.0, False, False, f"https://csfloat.com/item/{i}") for i in ids]
    c = Contract(cat, [ContractInput(l.skin, l.float_value, price=l.cost, source=l.market, link=l.link) for l in ls])
    res = evaluate(c, FlatPrices())
    res.listings, res.label = ls, "Phoenix | target FT"
    return res


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(w, "LOG_FILE", tmp_path / "alerts.log")
    return tmp_path


def test_alerts_once_per_listing_set_across_restarts(cat, files, monkeypatch):
    a = make_result(cat, [str(i) for i in range(10)])
    b = make_result(cat, [str(i) for i in range(10, 20)])
    scans = iter([[a], [a, b], [b]])
    notified = []
    monkeypatch.setattr(w, "run_scan", lambda *args, **kw: ScanOutcome(results=next(scans), deep_ran=True))
    monkeypatch.setattr(w, "notify", lambda title, text: notified.append(title))
    monkeypatch.setattr(w, "print_result", lambda *a, **k: None)
    monkeypatch.setattr(w, "Catalog", lambda: cat)
    run = lambda: w.watch(lambda: load_config("nonexistent.toml"), [4], [False], [("FT", 0.38)], True, 5, 0.05,
                          60, once=True)
    run()             # a is new
    run()             # a seen, b new
    run()             # a fresh process would reload the seen file: nothing new
    assert len(notified) == 2
    log = (files / "alerts.log").read_text(encoding="utf8")
    assert log.count("Phoenix | target FT") == 2 and "https://csfloat.com/item/15" in log
    assert len(w.load_seen()) == 2


def test_failed_scan_does_not_stop_watching(cat, files, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(w, "run_scan", boom)
    monkeypatch.setattr(w, "Catalog", lambda: cat)
    w.watch(lambda: load_config("nonexistent.toml"), [4], [False], [("FT", 0.38)], True, 5, 0.05, 60, once=True)


def test_result_key_ignores_order(cat):
    assert w.result_key(make_result(cat, ["2", "1"] + [str(i) for i in range(3, 11)])) == \
        w.result_key(make_result(cat, [str(i) for i in range(1, 11)]))
