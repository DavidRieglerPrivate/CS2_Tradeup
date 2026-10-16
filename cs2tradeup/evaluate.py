"""Turn a contract into money: outcome values, EV, ROI, profit chance."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .engine import Contract, Outcome
from .pricing import PriceBook, Quote


@dataclass
class ValuedOutcome:
    outcome: Outcome
    quote: Quote


@dataclass
class Result:
    contract: Contract
    outcomes: list[ValuedOutcome]
    cost: float
    ev: float               # expected net sell value
    missing_prices: int
    optimal: bool = True    # solver proved this is the best selection
    listings: list = field(default_factory=list)
    label: str = ""
    problem: object = None  # the optimizer Problem this came from, so it can be re-solved without sold listings
    sell_fee: float = 0.0
    sell_market: str = ""
    confirmed: bool = False  # listings were re-checked as still for sale after the scan

    @property
    def spread(self) -> float:
        """Standard deviation of the sell value of one trade-up (how much single results swing)."""
        return sum(v.outcome.probability * ((v.quote.net or 0) - self.ev) ** 2 for v in self.outcomes) ** 0.5

    @property
    def runs_needed(self) -> int | None:
        """Trade-ups needed to be ~95% sure the total comes out ahead (normal approximation); None if EV <= 0."""
        if self.profit <= 0:
            return None
        if self.spread == 0:
            return 1
        return max(1, math.ceil((1.645 * self.spread / self.profit) ** 2))

    @property
    def big_loss_chance(self) -> float:
        """Chance of getting back less than half of what the inputs cost."""
        return sum(v.outcome.probability for v in self.outcomes if (v.quote.net or 0) < self.cost / 2)

    @property
    def profit(self) -> float:
        return self.ev - self.cost

    @property
    def roi(self) -> float:
        return self.profit / self.cost if self.cost else 0.0

    @property
    def profit_chance(self) -> float:
        return sum(v.outcome.probability for v in self.outcomes if (v.quote.net or 0) > self.cost)

    @property
    def worst(self) -> float:
        return min((v.quote.net or 0) for v in self.outcomes) - self.cost

    @property
    def best(self) -> float:
        return max((v.quote.net or 0) for v in self.outcomes) - self.cost

    @property
    def max_output_float(self) -> float | None:
        fl = [v.outcome.float_value for v in self.outcomes if v.outcome.float_value is not None]
        return max(fl) if fl else None


def evaluate(contract: Contract, prices: PriceBook) -> Result:
    valued = []
    ev = 0.0
    missing = 0
    for o in contract.outcomes():
        q = prices.quote(o.variants, o.wear, contract.stattrak)
        if q.net is None:
            missing += 1
        else:
            ev += o.probability * q.net
        valued.append(ValuedOutcome(o, q))
    cost = sum(i.price for i in contract.inputs)
    pcfg = prices.cfg["pricing"]
    return Result(contract, valued, cost, ev, missing, sell_fee=float(pcfg["sell_fee"]),
                  sell_market=str(pcfg.get("sell_market", "")))
