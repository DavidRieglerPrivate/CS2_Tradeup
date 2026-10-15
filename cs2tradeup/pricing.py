"""Bulk price data: Skinport sales history and listings, Waxpeer lowest prices.

Used to value outputs (sales median) and for the coarse scan (cheapest price per wear tier).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .catalog import Skin, market_hash_name
from .config import Config
from .http import Http

SKINPORT_TTL = 600     # Skinport caches these endpoints for ~5 minutes and rate limits hard
WAXPEER_TTL = 300
WINDOWS = ["last_24_hours", "last_7_days", "last_30_days", "last_90_days"]
NO_SALES = "no sales history"


@dataclass
class Quote:
    price: float | None        # gross sell price estimate, USD
    net: float | None          # after the configured sell fee
    volume: int                # sales in the chosen window
    source: str
    illiquid: bool
    page: str = ""


@dataclass
class Offer:
    price: float
    quantity: int
    market: str
    page: str


class PriceBook:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sales: dict[tuple[str, str | None], dict] = {}
        self.skinport: dict[tuple[str, str | None], dict] = {}
        self.waxpeer: dict[str, dict] = {}
        self.reference: dict[str, float | None] = {}   # CSFloat reference prices, fetched on demand

    async def load(self, http: Http) -> None:
        base = {"app_id": 730, "currency": "USD"}
        hist = await http.get_json("skinport", "https://api.skinport.com/v1/sales/history",
                                   params=base, cache_key="skinport:history", ttl=SKINPORT_TTL)
        for row in hist:
            self.sales[(row["market_hash_name"], row.get("version"))] = row
        items = await http.get_json("skinport", "https://api.skinport.com/v1/items",
                                    params={**base, "tradable": 0}, cache_key="skinport:items", ttl=SKINPORT_TTL)
        for row in items:
            self.skinport[(row["market_hash_name"], row.get("version"))] = row
        try:
            wax = await http.get_json("waxpeer", "https://api.waxpeer.com/v1/prices",
                                      params={"game": "csgo"}, cache_key="waxpeer:prices", ttl=WAXPEER_TTL)
            for row in wax.get("items", []):
                self.waxpeer[row["name"]] = row
        except Exception:  # Waxpeer prices are only a fallback
            pass

    # ------------------------------------------------------------ outputs
    def _quote_one(self, name: str, version: str | None) -> Quote:
        pcfg = self.cfg["pricing"]
        fee = float(pcfg["sell_fee"])
        min_vol = int(pcfg["min_sales_volume"])
        start = WINDOWS.index(pcfg["median_window"]) if pcfg["median_window"] in WINDOWS else 1
        row = self.sales.get((name, version))
        page = (self.skinport.get((name, version)) or {}).get("item_page", "")
        lowest = self.lowest_listing(name, version)
        median = None
        if row:
            # shortest window with enough sales; one or two sales are too easy to distort
            usable = [(win, row.get(win) or {}) for win in WINDOWS[start:]]
            usable = [(win, w) for win, w in usable if w.get("median")]
            enough = [(win, w) for win, w in usable if int(w.get("volume") or 0) >= min_vol]
            pick = enough[0] if enough else (usable[-1] if usable else None)
            if pick:
                win, w = pick
                median = (w["median"], int(w.get("volume") or 0), f"skinport median {win}", not enough)
        if median:
            price, vol, src, illiquid = median
            if self.cfg["pricing"].get("cap_at_lowest_listing", True) and lowest and lowest[0] < price:
                price, src = lowest[0], f"capped at {lowest[1]}"
            return Quote(price, price * (1 - fee), vol, src, illiquid, page)
        # No sales history. A lone asking price is not a value (sellers list rare items at fantasy prices),
        # so use CSFloat's own reference price when it has been fetched, capped at the cheapest listing.
        ref = self.reference.get(name) if version is None else None
        if ref:
            price, src = ref, "csfloat reference price"
            if lowest and lowest[0] < price:
                price, src = lowest[0], f"capped at {lowest[1]}"
            return Quote(price, price * (1 - fee), 0, src, True, page)
        return Quote(None, None, 0, NO_SALES, True, page)

    def needs_reference(self, name: str, version: str | None) -> bool:
        if version is not None or name in self.reference:
            return False
        row = self.sales.get((name, version)) or {}
        return not any((row.get(w) or {}).get("median") for w in WINDOWS)

    async def fetch_references(self, http: Http, names: list[str]) -> None:
        """Look up CSFloat's reference price for outputs that never sold on Skinport."""
        key = self.cfg.key("csfloat")
        if not key:
            return

        async def one(name: str):
            try:
                data = await http.get_json(
                    "csfloat", "https://csfloat.com/api/v1/listings",
                    params={"market_hash_name": name, "limit": 1, "sort_by": "lowest_price", "type": "buy_now"},
                    headers={"Authorization": key}, cache_key=f"csfloat-ref:{name}", ttl=3600)
                rows = data.get("data", []) if isinstance(data, dict) else data
                ref = (rows[0].get("reference") or {}) if rows else {}
                base = ref.get("base_price") or ref.get("predicted_price")
                self.reference[name] = base / 100 if base else None
            except Exception:
                self.reference[name] = None

        await asyncio.gather(*(one(n) for n in dict.fromkeys(names)))

    def lowest_listing(self, name: str, version: str | None) -> tuple[float, str] | None:
        """Cheapest competing listing right now (what you'd have to undercut to sell)."""
        prices = []
        sp = self.skinport.get((name, version))
        if sp and sp.get("min_price") and sp.get("quantity"):
            prices.append((sp["min_price"], "skinport lowest listing"))
        wx = self.waxpeer.get(name)
        if wx and wx.get("min") and wx.get("count") and version is None:
            prices.append((wx["min"] / 1000, "waxpeer lowest listing"))
        return min(prices) if prices else None

    def quote(self, variants: list[Skin], wear: str | None, stattrak: bool) -> Quote:
        """Sell value of an outcome. Doppler-style groups are a phase-weighted average."""
        if len(variants) == 1 or not any(v.phase for v in variants):
            return self._quote_one(market_hash_name(variants[0], wear, stattrak), variants[0].phase)
        weights = self.cfg["pricing"]["doppler_phase_weights"]
        name = market_hash_name(variants[0], wear, stattrak)
        tot_w = tot_p = tot_n = 0.0
        vol = 0
        illiquid = False
        for v in variants:
            q = self._quote_one(name, v.phase)
            if q.price is None:
                continue
            w = float(weights.get(v.phase, 0.1))
            tot_w += w
            tot_p += w * q.price
            tot_n += w * q.net
            vol += q.volume
            illiquid |= q.illiquid
        if not tot_w:
            return self._quote_one(name, None)
        return Quote(tot_p / tot_w, tot_n / tot_w, vol, "skinport phase-weighted", illiquid,
                     (self.skinport.get((name, variants[0].phase)) or {}).get("item_page", ""))

    # ------------------------------------------------------------ inputs (coarse)
    def listed_anywhere(self, skin: Skin, stattrak: bool, souvenir: bool) -> bool:
        """Whether the bulk price lists show any listing of this skin version in any wear."""
        from .wear import possible_wears

        return any(self.cheapest(skin, w, stattrak, souvenir) for w in possible_wears(skin.min_float, skin.max_float))

    def cheapest(self, skin: Skin, wear: str, stattrak: bool, souvenir: bool) -> Offer | None:
        """Lowest current listing for a skin in one wear tier across bulk sources."""
        name = market_hash_name(skin, wear, stattrak, souvenir)
        best = None
        sp = self.skinport.get((name, None))
        if sp and sp.get("min_price") and sp.get("quantity"):
            best = Offer(sp["min_price"], sp["quantity"], "skinport", sp.get("item_page", ""))
        wx = self.waxpeer.get(name)
        if wx and wx.get("min") and wx.get("count"):
            p = wx["min"] / 1000
            if best is None or p < best.price:
                best = Offer(p, wx["count"], "waxpeer", "")
        return best
