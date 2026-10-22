"""Skins the user already owns, read from their public Steam inventory.

steamcommunity.com/inventory/{steamid64}/730/2 answers without an API key when the profile and its inventory
are public. Every asset's float comes along in `asset_properties` (propertyid 2, "Wear Rating"), so no inspect
service is needed. Skins inside storage units are not part of it.

Owned skins become Listings of the market OWNED, so the optimizer mixes them with market listings. Each one costs
what it would sell for (`[inventory] owned_cost`): using it in a trade-up means giving up that money.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import Catalog, Skin, market_hash_name, split_item_name
from .config import Config
from .http import Http, HttpError
from .markets import OWNED, Listing
from .pricing import PriceBook
from .wear import wear_of

URL = "https://steamcommunity.com/inventory/{}/730/2"
PAGE = 2000           # largest page Steam accepts; 5000 answers HTTP 400 (measured 2026-10-21)
MAX_PAGES = 20
WEAR_RATING = 2       # asset_properties propertyid of the float
INVENTORY_TTL = 600   # Steam rate limits inventory reads hard; a scan and a re-check share one read
STEAM_ID_RE = re.compile(r"^7656119\d{10}$")


class InventoryError(RuntimeError):
    pass


@dataclass
class OwnedItem:
    asset_id: str
    name: str            # market hash name
    skin: Skin
    float_value: float
    stattrak: bool
    souvenir: bool
    tradable: bool


async def resolve_steam_id(http: Http, text: str) -> str:
    """SteamID64 from a SteamID64, a profile link (/profiles/<id> or /id/<name>) or a custom profile name."""
    text = text.strip().rstrip("/")
    if STEAM_ID_RE.match(text):
        return text
    m = re.search(r"/profiles/(7656119\d{10})", text)
    if m:
        return m.group(1)
    name = re.sub(r"^.*/id/", "", text)
    if not name or "/" in name:
        raise InventoryError(f"'{text}' is not a Steam profile link, SteamID64 or profile name")
    try:
        r = await http.client.get(f"https://steamcommunity.com/id/{name}/", params={"xml": 1})
    except Exception as e:
        raise InventoryError(f"could not reach Steam to look up '{name}': {e}") from e
    m = re.search(r"<steamID64>(\d{17})</steamID64>", r.text)
    if not m:
        raise InventoryError(f"no Steam profile named '{name}'. Paste the link to your profile instead.")
    return m.group(1)


async def fetch_pages(http: Http, steam_id: str) -> list[dict]:
    """Every page of the CS2 inventory (raw JSON)."""
    pages = []
    start = None
    for _ in range(MAX_PAGES):
        params = {"l": "english", "count": PAGE}
        if start:
            params["start_assetid"] = start
        try:
            data = await http.get_json("steam", URL.format(steam_id), params=params, ttl=INVENTORY_TTL,
                                       cache_key=f"steam-inventory:{steam_id}:{start}")
        except HttpError as e:
            if e.status in (401, 403):
                raise InventoryError("this Steam inventory is private. Set Steam > Profile > Edit Profile > "
                                     "Privacy Settings > Inventory to Public.") from e
            if e.status == 429:
                raise InventoryError("Steam is refusing inventory requests for a while (too many). "
                                     "Try again in a few minutes.") from e
            raise InventoryError(f"could not read the Steam inventory: {e}") from e
        if not isinstance(data, dict) or data.get("success") != 1:
            raise InventoryError("Steam did not return the inventory (the profile may be private or not exist)")
        pages.append(data)
        # a page can come back without assets but with more_items set, so the count alone doesn't end it
        if not data.get("more_items") or not data.get("last_assetid"):
            break
        start = data["last_assetid"]
    return pages


def parse_inventory(pages: list[dict], catalog: Catalog) -> tuple[list[OwnedItem], int]:
    """(weapon skins with a float that the catalog knows, number of items in the inventory)."""
    items: list[OwnedItem] = []
    total = 0
    for data in pages:
        descs = {(d["classid"], d.get("instanceid", "0")): d for d in data.get("descriptions") or []}
        floats = {}
        for p in data.get("asset_properties") or []:
            for prop in p.get("asset_properties") or []:
                if prop.get("propertyid") == WEAR_RATING and prop.get("float_value") is not None:
                    floats[p["assetid"]] = float(prop["float_value"])
        for a in data.get("assets") or []:
            total += 1
            d = descs.get((a["classid"], a.get("instanceid", "0")))
            fl = floats.get(a["assetid"])
            if d is None or fl is None:   # cases, stickers, agents, ...
                continue
            name = d.get("market_hash_name") or ""
            # the Quality tag can't tell them apart ("highlight" items are Souvenir or StatTrak), the name can
            base, stattrak, souvenir, _ = split_item_name(name)
            skins = [s for s in catalog.by_name.get(base.lower(), []) if not s.is_special]
            if not skins:   # knives, gloves, or a skin newer than the catalog
                continue
            items.append(OwnedItem(str(a["assetid"]), name, skins[0], fl, stattrak, souvenir,
                                   bool(d.get("tradable", 1))))
    return items, total


def owned_listings(items: list[OwnedItem], prices: PriceBook, cfg: Config) -> list[Listing]:
    """Owned skins as Listings, each costing what it would sell for (or nothing, with owned_cost = "free")."""
    free = str(cfg["inventory"].get("owned_cost", "sell")).lower() == "free"
    out = []
    for it in items:
        value = 0.0 if free else prices.sell_value(it.skin, wear_of(it.float_value), it.stattrak, it.souvenir)
        out.append(Listing(market=OWNED, listing_id=it.asset_id, skin=it.skin, name=it.name,
                           float_value=it.float_value, price=round(value, 4), cost=round(value, 4),
                           stattrak=it.stattrak, souvenir=it.souvenir, link="", trade_locked=not it.tradable))
    return out


async def load_owned(http: Http, cfg: Config, catalog: Catalog, prices: PriceBook,
                     steam_id: str | None = None) -> tuple[list[Listing], int]:
    """(owned skins usable as trade-up inputs, items in the inventory). Raises InventoryError with a plain reason."""
    raw = steam_id or cfg.key("steam_id")
    if not raw:
        raise InventoryError("no Steam profile is set. Add it in the menu (option 5) or as steam_id in config.toml.")
    sid = await resolve_steam_id(http, raw)
    items, total = parse_inventory(await fetch_pages(http, sid), catalog)
    # only skins with a next rarity in their collection (a Covert from a case-less collection has none)
    usable = [it for it in items
              if (col := catalog.collection_of(it.skin)) and col.outputs_for(it.skin.tier, it.stattrak)]
    owned = owned_listings(usable, prices, cfg)
    owned.sort(key=lambda l: (l.skin.tier, l.stattrak, l.skin.collection_id or "", l.skin.name, l.float_value))
    return owned, total


def fits(listing: Listing, tier: int, stattrak: bool, souvenir_ok: bool = True) -> bool:
    """Whether an owned skin can go into a contract of this rarity and StatTrak mode."""
    return (listing.skin.tier == tier and listing.stattrak == stattrak
            and (souvenir_ok or not listing.souvenir))


def display_name(listing: Listing) -> str:
    return market_hash_name(listing.skin, wear_of(listing.float_value), listing.stattrak, listing.souvenir)
