"""Market adapters. Each returns individual listings (with floats) in one common format."""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .catalog import Skin, market_hash_name
from .config import Config
from .http import Http, HttpError
from .wear import WEARS, possible_wears

LISTING_TTL = 120  # seconds listings stay cached


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

    @property
    def available(self) -> bool:
        return True

    def _listing(self, **kw) -> Listing:
        kw["cost"] = round(kw["price"] * (1 + self.buy_fee), 4)
        return Listing(market=self.name, **kw)

    async def listings(self, skin: Skin, stattrak: bool, souvenir: bool, max_float: float) -> list[Listing]:
        raise NotImplementedError


class CSFloat(Market):
    """https://csfloat.com/api/v1/listings - needs an API key (Authorization header)."""
    name = "csfloat"
    URL = "https://csfloat.com/api/v1/listings"

    @property
    def available(self) -> bool:
        return bool(self.cfg.key("csfloat"))

    async def listings(self, skin, stattrak, souvenir, max_float, pages: int = 2):
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
            data = await self.http.get_json(self.name, self.URL, params=params, cache_key=key, ttl=LISTING_TTL,
                                            headers={"Authorization": self.cfg.key("csfloat")})
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
                                            ttl=LISTING_TTL, sign=self._sign)
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


class Waxpeer(Market):
    """https://api.waxpeer.com/v1/get-items-list - API key as query param, 20 req/min.

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

    async def listings(self, skin, stattrak, souvenir, max_float, pages: int = 3):
        out: list[Listing] = []
        for w in _wears_in_band(skin, max_float):
            name = market_hash_name(skin, w, stattrak, souvenir)
            found = 0
            for page in range(pages):
                params = {"api": self.cfg.key("waxpeer"), "game": "csgo", "search": name,
                          "order_by": "price", "order": "asc", "skip": page * self.PAGE}
                if self.include_locked:
                    params["include_hold"] = 1
                data = await self.http.get_json(
                    self.name, self.URL, params=params, ttl=LISTING_TTL,
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
                    found += 1
                    out.append(self._listing(
                        listing_id=str(row["item_id"]), skin=skin, name=name,
                        float_value=float(fl), price=row["price"] / 1000, stattrak=stattrak, souvenir=souvenir,
                        trade_locked=bool(row.get("unlock_at")),
                        link=f"https://waxpeer.com/?game=csgo&search={quote(name)}#item_id={row['item_id']}"))
                if found >= self.WANTED or len(rows) < self.PAGE:
                    break
        return out


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
