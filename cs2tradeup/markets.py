"""Market adapters. Each returns individual listings (with floats) in one common format."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx

from .catalog import Skin, market_hash_name
from .config import Config
from .http import Http, HttpError
from .wear import WEARS, possible_wears


@dataclass
class Listing:
    market: str
    listing_id: str
    skin: Skin
    name: str            # market hash name
    float_value: float
    price: float         # USD, listing price
    cost: float          # USD, price + buy fees
    stattrak: bool
    souvenir: bool
    link: str
    trade_locked: bool = False


# DMarket's float buckets ("floatPart" filter), measured against live offers: (name, lower, upper).
DMARKET_FLOAT_PARTS = [
    ("FN-0", 0.00, 0.01), ("FN-1", 0.01, 0.02), ("FN-2", 0.02, 0.03), ("FN-3", 0.03, 0.04),
    ("FN-4", 0.04, 0.05), ("FN-5", 0.05, 0.06), ("FN-6", 0.06, 0.07),
    ("MW-0", 0.07, 0.08), ("MW-1", 0.08, 0.09), ("MW-2", 0.09, 0.10), ("MW-3", 0.10, 0.11), ("MW-4", 0.11, 0.15),
    ("FT-0", 0.15, 0.18), ("FT-1", 0.18, 0.21), ("FT-2", 0.21, 0.24), ("FT-3", 0.24, 0.27), ("FT-4", 0.27, 0.38),
    ("WW-0", 0.38, 0.39), ("WW-1", 0.39, 0.40), ("WW-2", 0.40, 0.41), ("WW-3", 0.41, 0.42), ("WW-4", 0.42, 0.45),
    ("BS-0", 0.45, 0.50), ("BS-1", 0.50, 0.63), ("BS-2", 0.63, 0.76), ("BS-3", 0.76, 0.80), ("BS-4", 0.80, 1.00),
]


def dmarket_float_parts(min_float: float, max_float: float) -> list[str]:
    """DMarket float buckets that overlap [min_float, max_float]."""
    return [name for name, lo, hi in DMARKET_FLOAT_PARTS if lo <= max_float and hi > min_float]


def _wears_in_band(skin: Skin, max_float: float) -> list[str]:
    return [c for c in possible_wears(skin.min_float, skin.max_float)
            if next(lo for code, _, lo, _ in WEARS if code == c) <= max_float]


class Market:
    name = "base"

    def __init__(self, http: Http, cfg: Config):
        self.http = http
        self.cfg = cfg
        self.buy_fee = float(cfg["buy_fees"].get(self.name, 0.0))
        self.include_locked = bool(cfg["markets"].get("include_trade_locked", False))
        # listings are reused across scans for this long, so repeated scans don't ask for the same pages again
        self.listing_ttl = float(cfg["scan"].get("listing_cache_minutes", 30)) * 60

    @property
    def available(self) -> bool:
        return True

    def resume_at(self) -> float | None:
        """When a used-up request limit lets this market answer again (epoch seconds), None if it isn't limited."""
        t = self.http.blocked_until.get(self.name, 0.0)
        return t if t > time.time() else None

    def _listing(self, **kw) -> Listing:
        kw["cost"] = round(kw["price"] * (1 + self.buy_fee), 4)
        return Listing(market=self.name, **kw)

    async def listings(self, skin: Skin, stattrak: bool, souvenir: bool, max_float: float) -> list[Listing]:
        raise NotImplementedError

    async def gone(self, listings: list[Listing]) -> set[str]:
        """IDs of listings that were sold, delisted or repriced since they were fetched.

        A listing the market can't confirm either way (request failed) is not reported as gone.
        """
        return set()


class CSFloat(Market):
    """https://csfloat.com/api/v1/listings - needs an API key (Authorization header)."""
    name = "csfloat"
    URL = "https://csfloat.com/api/v1/listings"

    def __init__(self, http: Http, cfg: Config):
        super().__init__(http, cfg)
        # the last requests of a window are kept for re-checking chosen listings (one request per listing)
        self.reserve = int(cfg["scan"].get("csfloat_reserve", 30))

    @property
    def available(self) -> bool:
        return bool(self.cfg.key("csfloat"))

    def _reserve_reached(self) -> float | None:
        """Reset time when the quota is down to the reserve, else None."""
        q = self.http.quota.get(self.name)
        if q and q["remaining"] <= self.reserve and q.get("reset_at") and q["reset_at"] > time.time():
            return q["reset_at"]
        return None

    def resume_at(self) -> float | None:
        return super().resume_at() or self._reserve_reached()

    async def listings(self, skin, stattrak, souvenir, max_float, pages: int = 2):
        reset = self._reserve_reached()
        if reset:
            raise HttpError(f"csfloat: request limit nearly used up (the last {self.reserve} are kept for "
                            f"re-checking listings), it resets at {datetime.fromtimestamp(reset):%H:%M}", 429)
        category = 2 if stattrak else 3 if souvenir else 1
        params = {
            "def_index": skin.def_index, "paint_index": skin.paint_index, "category": category,
            "min_float": skin.min_float, "max_float": round(max_float, 6),
            "sort_by": "lowest_price", "limit": 50, "type": "buy_now",
        }
        out: list[Listing] = []
        cursor = None
        for _ in range(pages):
            if cursor:
                params["cursor"] = cursor
            key = f"csfloat:{sorted(params.items())}"
            data = await self.http.get_json(self.name, self.URL, params=params, cache_key=key,
                                            ttl=self.listing_ttl, headers={"Authorization": self.cfg.key("csfloat")})
            rows = data.get("data", []) if isinstance(data, dict) else data
            for row in rows:
                item = row.get("item", {})
                fl = item.get("float_value")
                if fl is None or row.get("type", "buy_now") != "buy_now":
                    continue
                out.append(self._listing(
                    listing_id=str(row["id"]), skin=skin,
                    name=item.get("market_hash_name", market_hash_name(skin, None, stattrak, souvenir)),
                    float_value=float(fl), price=row["price"] / 100, stattrak=stattrak, souvenir=souvenir,
                    link=f"https://csfloat.com/item/{row['id']}"))
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not cursor or len(rows) < 50:
                break
        return out

    async def gone(self, listings):
        async def one(l: Listing) -> str | None:
            try:
                row = await self.http.get_json(self.name, f"{self.URL}/{l.listing_id}",
                                               headers={"Authorization": self.cfg.key("csfloat")})
            except HttpError as e:
                return l.listing_id if e.status in (400, 404, 410) else None   # unknown resource = gone
            if row.get("state") != "listed" or row.get("price") / 100 != l.price:
                return l.listing_id
            return None

        return {r for r in await asyncio.gather(*(one(l) for l in listings)) if r}


class DMarket(Market):
    """https://api.dmarket.com/marketplace-api/v2/offers - signed with an Ed25519 key pair."""
    name = "dmarket"
    BASE = "https://api.dmarket.com"
    PATH = "/marketplace-api/v2/offers"

    @property
    def available(self) -> bool:
        return bool(self.cfg.key("dmarket_public") and self.cfg.key("dmarket_secret"))

    def _sign(self, url: str) -> dict:
        from nacl.signing import SigningKey

        u = httpx.URL(url)
        path = u.raw_path.decode()
        ts = str(int(time.time()))
        secret = bytes.fromhex(self.cfg.key("dmarket_secret"))[:32]
        sig = SigningKey(secret).sign(("GET" + path + ts).encode()).signature.hex()
        return {"X-Api-Key": self.cfg.key("dmarket_public"), "X-Sign-Date": ts,
                "X-Request-Sign": "dmar ed25519 " + sig}

    async def listings(self, skin, stattrak, souvenir, max_float, pages: int = 3):
        category = "stattrak_tm" if stattrak else "souvenir" if souvenir else "normal"
        # filter by float bucket on the server: sorted by price alone, the cheapest offers are the
        # high floats of each wear, and every one of them would be dropped below
        parts = dmarket_float_parts(skin.min_float, max_float)
        filters = [f"category={category}"] + [f"floatPart[]={p}" for p in parts]
        title = market_hash_name(skin, None, stattrak, souvenir)
        params = {"gameId": "a8db", "title": title, "limit": 100, "orderBy": "price", "orderDir": "asc",
                  "offerType": "all", "treeFilters": ",".join(filters)}
        out: list[Listing] = []
        cursor = None
        for _ in range(pages):
            if cursor:
                params["cursor"] = cursor
            key = f"dmarket:{sorted(params.items())}"
            data = await self.http.get_json(self.name, self.BASE + self.PATH, params=params, cache_key=key,
                                            ttl=self.listing_ttl, sign=self._sign)
            items = data.get("items", [])
            for it in items:
                attrs = it.get("attributes", {})
                cs2 = attrs.get("cs2", {})
                if cs2.get("paintIndex") != skin.paint_index or cs2.get("defIndex") != skin.def_index:
                    continue
                locked = (bool(attrs.get("tradeLockDays")) or bool(it.get("locked"))
                          or not attrs.get("tradable", True))
                if locked and not self.include_locked:
                    continue
                try:
                    fl = float(cs2.get("float"))
                except (TypeError, ValueError):
                    continue
                if fl > max_float:
                    continue
                offer = it.get("offerId", "")
                ttl_name = attrs.get("title", title)
                out.append(self._listing(
                    listing_id=offer, skin=skin, name=ttl_name, float_value=fl,
                    price=int(it["priceCents"]) / 100, stattrak=stattrak, souvenir=souvenir, trade_locked=locked,
                    link=f"https://dmarket.com/ingame-items/item-list/csgo-skins?title={quote(ttl_name)}"
                         f"&userOfferId={offer}"))
            cursor = data.get("cursor")
            if not cursor or len(items) < 100:
                break
        return out

    async def gone(self, listings):
        # DMarket has no lookup by offer id: search again in each listing's own float bucket
        groups: dict[tuple, list[Listing]] = {}
        for l in listings:
            groups.setdefault((l.skin.id, l.stattrak, l.souvenir), []).append(l)

        async def one(group: list[Listing]) -> set[str]:
            l0 = group[0]
            category = "stattrak_tm" if l0.stattrak else "souvenir" if l0.souvenir else "normal"
            parts = sorted({p for l in group for p in dmarket_float_parts(l.float_value, l.float_value)})
            params = {"gameId": "a8db", "title": market_hash_name(l0.skin, None, l0.stattrak, l0.souvenir),
                      "limit": 100, "orderBy": "price", "orderDir": "asc", "offerType": "all",
                      "treeFilters": ",".join([f"category={category}"] + [f"floatPart[]={p}" for p in parts])}
            live: dict[str, float] = {}
            try:
                for _ in range(5):
                    data = await self.http.get_json(self.name, self.BASE + self.PATH, params=params, sign=self._sign)
                    items = data.get("items", [])
                    live.update({it.get("offerId"): int(it["priceCents"]) / 100 for it in items})
                    if all(l.listing_id in live for l in group) or not data.get("cursor") or len(items) < 100:
                        break
                    params["cursor"] = data["cursor"]
            except HttpError:
                return set()
            return {l.listing_id for l in group if live.get(l.listing_id) != l.price}

        return set().union(*await asyncio.gather(*(one(g) for g in groups.values()))) if groups else set()


class Waxpeer(Market):
    """https://api.waxpeer.com/v1/get-items-list - API key as query param (no documented limit; 5/s worked).

    Waxpeer has no float filter, so listings come cheapest first and are filtered here. A page holds 100
    rows (search-items-by-name stops at 50); more pages are read only while too few rows fit the float band.
    """
    name = "waxpeer"
    URL = "https://api.waxpeer.com/v1/get-items-list"
    PAGE = 100
    WANTED = 10   # stop paging once this many listings fit the band

    @property
    def available(self) -> bool:
        return bool(self.cfg.key("waxpeer"))

    async def listings(self, skin, stattrak, souvenir, max_float, pages: int = 3, wears: list[str] | None = None):
        """`wears` limits the search to those wear names (each wear is its own search here)."""
        out: list[Listing] = []
        for w in _wears_in_band(skin, max_float):
            if wears is not None and w not in wears:
                continue
            name = market_hash_name(skin, w, stattrak, souvenir)
            found = 0
            for page in range(pages):
                params = {"api": self.cfg.key("waxpeer"), "game": "csgo", "search": name,
                          "order_by": "price", "order": "asc", "skip": page * self.PAGE}
                if self.include_locked:
                    params["include_hold"] = 1
                data = await self.http.get_json(
                    self.name, self.URL, params=params, ttl=self.listing_ttl,
                    cache_key=f"waxpeer-list:{name}:{page}:{self.include_locked}")
                if not data.get("success", True):
                    raise HttpError(f"waxpeer: {data.get('msg')}")
                rows = data.get("items") or []
                for row in rows:
                    # the search is a substring match: it also returns the StatTrak/Souvenir versions
                    if row.get("name") != name:
                        continue
                    fl = row.get("float")
                    if fl is None or fl > max_float:
                        continue
                    locked = bool(row.get("unlock_at"))   # only sent for held items, with include_hold
                    if locked and not self.include_locked:
                        continue
                    found += 1
                    out.append(self._listing(
                        listing_id=str(row["item_id"]), skin=skin, name=name,
                        float_value=float(fl), price=row["price"] / 1000, stattrak=stattrak, souvenir=souvenir,
                        trade_locked=locked,
                        link=f"https://waxpeer.com/?game=csgo&search={quote(name)}#item_id={row['item_id']}"))
                if found >= self.WANTED or len(rows) < self.PAGE:
                    break
        return out

    async def gone(self, listings):
        ids = [l.listing_id for l in listings]
        live: dict[str, float] = {}
        try:
            for i in range(0, len(ids), 50):
                data = await self.http.get_json(self.name, "https://api.waxpeer.com/v1/check-availability",
                                                params={"api": self.cfg.key("waxpeer"), "item_id": ids[i:i + 50]})
                # sold or unknown items are simply left out of the answer
                live.update({str(r["item_id"]): r["price"] / 1000 for r in data.get("items", []) if r.get("selling")})
        except HttpError:
            return set()
        return {l.listing_id for l in listings if live.get(l.listing_id) != l.price}


MARKETS = {"csfloat": CSFloat, "dmarket": DMarket, "waxpeer": Waxpeer}


def build_markets(http: Http, cfg: Config) -> list[Market]:
    out = []
    for name in cfg["markets"]["enabled"]:
        cls = MARKETS.get(name)
        if cls:
            m = cls(http, cfg)
            if m.available:
                out.append(m)
    return out
