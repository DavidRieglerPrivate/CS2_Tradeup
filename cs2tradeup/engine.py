"""Trade-up contract mechanics: validation, outcome odds, output floats and float budgets."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .catalog import COVERT, SPECIAL, Catalog, Collection, Skin
from .wear import f32, wear_of


class ContractError(ValueError):
    pass


def contract_size(tier: int) -> int:
    """10 inputs for normal contracts, 5 Coverts for a knife/glove contract."""
    return 5 if tier == COVERT else 10


@dataclass
class ContractInput:
    skin: Skin
    float_value: float
    souvenir: bool = False
    price: float = 0.0          # USD incl. buy fees, optional
    source: str = ""            # market name, optional
    link: str = ""


@dataclass
class Outcome:
    name: str                    # e.g. "AK-47 | Redline" or "★ Karambit | Doppler"
    variants: list[Skin]         # >1 for Doppler phases
    probability: float
    float_value: float | None    # None for vanilla knives
    wear: str | None
    collections: set[str] = field(default_factory=set)

    @property
    def skin(self) -> Skin:
        return self.variants[0]


def normalize(skin: Skin, fl: float, formula: str = "normalized") -> float:
    """An input's contribution to the contract average."""
    if formula == "legacy":
        return fl
    rng = skin.float_range
    if rng <= 0:
        return 0.0
    return min(1.0, max(0.0, (fl - skin.min_float) / rng))


def output_float(out: Skin, avg: float) -> float | None:
    """Map the average input value onto the output's own float range (this is the float cap)."""
    if out.is_vanilla:
        return None
    return f32(out.min_float + avg * out.float_range)


def max_avg_for(out: Skin, max_out_float: float) -> float:
    """Largest contract average that keeps this output at or below max_out_float.

    Returns >= 1.0 when every input combination is fine (output capped below target),
    and a negative number when the target is impossible (output's min float is above it).
    """
    if out.is_vanilla or out.max_float <= max_out_float:
        return 1.0
    if out.float_range <= 0:
        return 1.0 if out.min_float <= max_out_float else -1.0
    return (max_out_float - out.min_float) / out.float_range


def outcome_pool(catalog: Catalog, col: Collection, tier: int, stattrak: bool) -> list[list[Skin]]:
    return col.outputs_for(tier, stattrak)


class Contract:
    def __init__(self, catalog: Catalog, inputs: list[ContractInput], stattrak: bool = False,
                 formula: str = "normalized"):
        self.catalog = catalog
        self.inputs = inputs
        self.stattrak = stattrak
        self.formula = formula
        self.validate()

    @property
    def tier(self) -> int:
        return self.inputs[0].skin.tier

    @property
    def size(self) -> int:
        return contract_size(self.tier)

    def validate(self) -> None:
        if not self.inputs:
            raise ContractError("contract has no inputs")
        tiers = {i.skin.tier for i in self.inputs}
        if len(tiers) != 1:
            raise ContractError("all inputs must be the same rarity")
        tier = tiers.pop()
        if tier >= SPECIAL:
            raise ContractError("knives and gloves can't be used as inputs")
        if len(self.inputs) != contract_size(tier):
            raise ContractError(f"this rarity needs exactly {contract_size(tier)} inputs, got {len(self.inputs)}")
        for i in self.inputs:
            if self.stattrak and i.souvenir:
                raise ContractError("StatTrak and Souvenir inputs can't be mixed")
            if self.stattrak and not i.skin.stattrak:
                raise ContractError(f"{i.skin.name} has no StatTrak version")
            if i.souvenir and not i.skin.souvenir:
                raise ContractError(f"{i.skin.name} has no Souvenir version")
            if not i.skin.is_vanilla and not (i.skin.min_float <= i.float_value <= i.skin.max_float):
                raise ContractError(f"{i.skin.name}: float {i.float_value} is outside its range "
                                    f"{i.skin.min_float}-{i.skin.max_float}")
            col = self.catalog.collection_of(i.skin)
            if col is None or not col.outputs_for(tier, self.stattrak):
                raise ContractError(f"{i.skin.name} can't be traded up (no higher-tier outcome in its collection)")

    def average(self) -> float:
        return sum(normalize(i.skin, i.float_value, self.formula) for i in self.inputs) / self.size

    def outcomes(self) -> list[Outcome]:
        avg = self.average()
        probs: dict[str, float] = defaultdict(float)
        groups: dict[str, list[Skin]] = {}
        cols: dict[str, set[str]] = defaultdict(set)
        for inp in self.inputs:
            col = self.catalog.collection_of(inp.skin)
            pool = col.outputs_for(self.tier, self.stattrak)
            for g in pool:
                key = g[0].name
                probs[key] += 1.0 / (self.size * len(pool))
                groups[key] = g
                cols[key].add(col.name)
        result = []
        for key, p in probs.items():
            g = groups[key]
            fl = output_float(g[0], avg)
            result.append(Outcome(key, g, p, fl, wear_of(fl) if fl is not None else None, cols[key]))
        result.sort(key=lambda o: -o.probability)
        return result


def outcome_distribution(catalog: Catalog, collection_counts: dict[str, int], tier: int,
                         stattrak: bool) -> dict[str, tuple[list[Skin], float]]:
    """Outcome odds for a composition given as {collection_id: number_of_inputs}."""
    n = contract_size(tier)
    out: dict[str, tuple[list[Skin], float]] = {}
    for cid, cnt in collection_counts.items():
        pool = catalog.collections[cid].outputs_for(tier, stattrak)
        for g in pool:
            prev = out.get(g[0].name, (g, 0.0))[1]
            out[g[0].name] = (g, prev + cnt / (n * len(pool)))
    return out
