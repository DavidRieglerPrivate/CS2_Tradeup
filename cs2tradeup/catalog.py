"""Item catalog built from the ByMykel CSGO-API dataset.

Provides skins with float caps, rarity tier, collection and case, and the
knife/glove ("rare special") pool of every case.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import DATA_DIR

BASE_URL = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/"
FILES = ("skins.json", "collections.json", "crates.json")

TIER_NAMES = ["Consumer", "Industrial", "Mil-Spec", "Restricted", "Classified", "Covert", "Knife/Glove"]
SPECIAL = 6
COVERT = 5
_RARITY_TIER = {
    "rarity_common_weapon": 0,
    "rarity_uncommon_weapon": 1,
    "rarity_rare_weapon": 2,
    "rarity_mythical_weapon": 3,
    "rarity_legendary_weapon": 4,
    "rarity_ancient_weapon": 5,
    "rarity_ancient": 5,  # gloves ("Extraordinary"), reclassified as SPECIAL below
}
_SPECIAL_CATEGORIES = {"sfui_invpanel_filter_melee", "sfui_invpanel_filter_gloves"}
# Pseudo-collections in the dataset that are not real trade-up collections.
EXCLUDED_COLLECTIONS = {"Limited Edition Item"}


@dataclass(frozen=True)
class Skin:
    id: str
    name: str                 # e.g. "AK-47 | Redline", "★ Karambit | Doppler"
    tier: int
    min_float: float | None   # None for vanilla knives
    max_float: float | None
    def_index: int | None
    paint_index: int | None
    phase: str | None
    stattrak: bool
    souvenir: bool
    collection_id: str | None
    is_glove: bool = False

    @property
    def is_special(self) -> bool:
        return self.tier == SPECIAL

    @property
    def is_vanilla(self) -> bool:
        return self.min_float is None

    @property
    def float_range(self) -> float:
        return (self.max_float or 0) - (self.min_float or 0)


@dataclass
class Collection:
    id: str
    name: str
    case_id: str | None = None
    case_name: str | None = None
    tiers: dict[int, list[Skin]] = field(default_factory=dict)
    # Knife/glove outcomes of the collection's case, grouped by finish:
    # {"★ Karambit | Doppler": [Phase 1 skin, Phase 2 skin, ...], "★ Karambit | Fade": [skin]}
    special_pool: dict[str, list[Skin]] = field(default_factory=dict)

    def outputs_for(self, input_tier: int, stattrak: bool) -> list[list[Skin]]:
        """Possible outcomes (each a list of phase variants) for one input of this collection."""
        if input_tier == COVERT:
            groups = list(self.special_pool.values())
            if stattrak:  # gloves have no StatTrak version, so a StatTrak contract can't hit them
                groups = [g for g in groups if not g[0].is_glove]
            return groups
        # phases of one finish (Glock-18 | Gamma Doppler) share a single outcome slot, like Doppler knives
        groups: dict[str, list[Skin]] = {}
        for s in self.tiers.get(input_tier + 1, []):
            groups.setdefault(s.name, []).append(s)
        out = list(groups.values())
        if stattrak:
            out = [g for g in out if g[0].stattrak]
        return out


class Catalog:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.skins: dict[str, Skin] = {}
        self.by_name: dict[str, list[Skin]] = {}
        self.collections: dict[str, Collection] = {}
        self._load(data_dir)

    # ---------------------------------------------------------------- loading
    @staticmethod
    def download(data_dir: Path = DATA_DIR) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            for fname in FILES:
                r = client.get(BASE_URL + fname)
                r.raise_for_status()
                (data_dir / fname).write_bytes(r.content)

    def _load(self, data_dir: Path) -> None:
        if not all((data_dir / f).exists() for f in FILES):
            self.download(data_dir)
        raw_skins = json.loads((data_dir / "skins.json").read_text(encoding="utf8"))
        raw_cols = json.loads((data_dir / "collections.json").read_text(encoding="utf8"))
        raw_crates = json.loads((data_dir / "crates.json").read_text(encoding="utf8"))

        for s in raw_skins:
            rid = s["rarity"]["id"]
            if rid not in _RARITY_TIER:
                continue  # contraband etc.
            cat = s["category"]["id"]
            special = cat in _SPECIAL_CATEGORIES
            cols = s.get("collections") or []
            skin = Skin(
                id=s["id"],
                name=s["name"],
                tier=SPECIAL if special else _RARITY_TIER[rid],
                min_float=s.get("min_float"),
                max_float=s.get("max_float"),
                def_index=(s.get("weapon") or {}).get("weapon_id"),
                paint_index=int(s["paint_index"]) if s.get("paint_index") not in (None, "") else None,
                phase=s.get("phase"),
                stattrak=bool(s.get("stattrak")),
                souvenir=bool(s.get("souvenir")),
                collection_id=cols[0]["id"] if cols else None,
                is_glove=cat == "sfui_invpanel_filter_gloves",
            )
            self.skins[skin.id] = skin
            self.by_name.setdefault(skin.name.lower(), []).append(skin)

        # weapon cases, plus newer containers (e.g. "Sealed ... Terminal") that carry a knife/glove pool
        cases = {c["id"]: c for c in raw_crates
                 if c.get("type") == "Case" or (c.get("type") is None and c.get("contains_rare"))}
        for c in raw_cols:
            if c["name"] in EXCLUDED_COLLECTIONS:
                continue
            col = Collection(id=c["id"], name=c["name"])
            for ref in c.get("contains", []):
                sk = self.skins.get(ref["id"])
                if sk and not sk.is_special:
                    col.tiers.setdefault(sk.tier, []).append(sk)
            case = next((cases[k["id"]] for k in c.get("crates", []) if k["id"] in cases), None)
            if case:
                col.case_id, col.case_name = case["id"], case["name"]
                for ref in case.get("contains_rare", []):
                    sk = self.skins.get(ref["id"])
                    if sk:
                        col.special_pool.setdefault(sk.name, []).append(sk)
            if col.tiers:
                self.collections[col.id] = col

    # ---------------------------------------------------------------- lookup
    def collection_of(self, skin: Skin) -> Collection | None:
        return self.collections.get(skin.collection_id) if skin.collection_id else None

    def find_skin(self, query: str) -> Skin:
        """Look up a tradeable weapon skin by (case-insensitive) name, e.g. 'AK-47 | Redline'."""
        q = query.strip().lower()
        hits = [s for s in self.by_name.get(q, []) if not s.is_special]
        if not hits:
            hits = [s for name, ss in self.by_name.items() if q in name for s in ss if not s.is_special]
            names = sorted({s.name for s in hits})
            if len(names) > 1:
                raise KeyError(f"'{query}' is ambiguous: {', '.join(names[:8])}")
        if not hits:
            raise KeyError(f"no skin named '{query}'")
        return hits[0]

    def find_collection(self, query: str) -> Collection:
        q = query.strip().lower()
        exact = [c for c in self.collections.values()
                 if q in (c.name.lower(), "the " + c.name.lower(), (c.case_name or "").lower())
                 or c.name.lower() in (q, "the " + q, "the " + q + " collection")]
        if exact:
            return exact[0]
        hits = [c for c in self.collections.values() if q in c.name.lower()
                or (c.case_name and q in c.case_name.lower())]
        if len(hits) != 1:
            names = ", ".join(c.name for c in hits[:8]) or "none"
            raise KeyError(f"collection '{query}' matched {len(hits)}: {names}")
        return hits[0]

    def search(self, query: str, tier: int | None = None, limit: int = 9,
               skins: bool = True, collections: bool = True) -> list[tuple[str, object]]:
        """Forgiving search over tradeable skins and collections.

        Returns [("skin", Skin) | ("collection", Collection), ...] best match first. Typos are tolerated.
        """
        import difflib

        q = " ".join(query.lower().replace("|", " ").split())
        if not q:
            return []
        words = q.split()
        scored: list[tuple[float, str, object]] = []

        def score(text: str) -> float:
            t = " ".join(text.lower().replace("|", " ").split())
            if t == q:
                return 3.0
            if all(w in t for w in words):
                return 2.0 + (0.5 if t.startswith(q) else 0) - len(t) / 1000
            if q.replace(" ", "") in t.replace(" ", ""):  # "chroma2" -> "chroma 2"
                return 1.9 - len(t) / 1000
            # typo tolerance: every typed word must closely match some word of the name
            tw = t.split()
            if not tw:
                return 0.0
            per = [max((1.0 if w in x else difflib.SequenceMatcher(None, w, x).ratio()) for x in tw)
                   for w in words]
            if min(per) >= 0.75:
                return 1.0 + sum(per) / len(per) / 2
            return 0.0

        if skins:
            seen = set()
            for s in self.skins.values():
                if s.is_special or s.name in seen or s.collection_id not in self.collections:
                    continue
                if tier is not None and s.tier != tier:
                    continue
                seen.add(s.name)
                sc = score(s.name)
                if sc >= 0.6:
                    scored.append((sc, "skin", s))
        if collections:
            for c in self.collections.values():
                if tier is not None and not c.tiers.get(tier):
                    continue
                sc = max(score(c.name), score(c.case_name or ""))
                if sc >= 0.6:
                    scored.append((sc + 0.01, "collection", c))
        scored.sort(key=lambda x: -x[0])
        return [(k, o) for _, k, o in scored[:limit]]

    def tradeup_collections(self, tier: int, stattrak: bool = False) -> list[Collection]:
        """Collections where a skin of this tier can be traded up (there is a higher outcome)."""
        out = []
        for col in self.collections.values():
            if not col.tiers.get(tier):
                continue
            if col.outputs_for(tier, stattrak):
                out.append(col)
        return out


def market_hash_name(skin: Skin, wear_code: str | None, stattrak: bool = False, souvenir: bool = False) -> str:
    """Steam market hash name, e.g. '★ StatTrak™ Karambit | Doppler (Factory New)'."""
    from .wear import wear_name

    name = skin.name
    if stattrak:
        name = name.replace("★ ", "★ StatTrak™ ", 1) if name.startswith("★ ") else "StatTrak™ " + name
    elif souvenir:
        name = "Souvenir " + name
    if wear_code and not skin.is_vanilla:
        name += f" ({wear_name(wear_code)})"
    return name
