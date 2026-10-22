"""Configuration loading: config.toml merged over built-in defaults, keys from env vars."""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"

DEFAULTS: dict = {
    "keys": {"csfloat": "", "waxpeer": "", "dmarket_public": "", "dmarket_secret": "", "steam_id": ""},
    "engine": {"float_formula": "normalized", "float_safety_margin": 0.000005},
    "pricing": {
        "median_window": "last_7_days",
        "min_sales_volume": 3,
        "cap_at_lowest_listing": True,
        "sell_fee": 0.02,
        "sell_market": "CSFloat",
        "doppler_phase_weights": {
            "Phase 1": 0.2, "Phase 2": 0.2, "Phase 3": 0.2, "Phase 4": 0.2,
            "Ruby": 0.06, "Sapphire": 0.06, "Black Pearl": 0.08, "Emerald": 0.2,
        },
    },
    "buy_fees": {"csfloat": 0.0, "dmarket": 0.0, "waxpeer": 0.0, "skinport": 0.0},
    "markets": {"enabled": ["csfloat", "dmarket", "waxpeer"], "include_trade_locked": False},
    "rate_limits": {"csfloat": 3.0, "dmarket": 10.0, "waxpeer": 3.0, "skinport": 0.025, "steam": 0.5},
    "inventory": {"owned_cost": "sell"},
    "scan": {
        "top_candidates": 0, "time_limit_minutes": 10, "max_results": 10, "min_roi": 0.0,
        "max_contract_cost": 0, "fetch_float_slack": 2.0, "confirm_top": 3,
        "listing_cache_minutes": 30, "csfloat_reserve": 30,
    },
}

ENV_KEYS = {
    "csfloat": "CSFLOAT_API_KEY",
    "waxpeer": "WAXPEER_API_KEY",
    "dmarket_public": "DMARKET_PUBLIC_KEY",
    "dmarket_secret": "DMARKET_SECRET_KEY",
    "steam_id": "STEAM_ID",
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    def __getitem__(self, section: str) -> dict:
        return self.raw[section]

    def key(self, name: str) -> str:
        return os.environ.get(ENV_KEYS[name], "") or self.raw["keys"].get(name, "")

    @property
    def float_formula(self) -> str:
        return self.raw["engine"]["float_formula"]

    @property
    def safety_margin(self) -> float:
        return float(self.raw["engine"]["float_safety_margin"])


def save_keys(keys: dict[str, str], path: str | Path | None = None) -> Path:
    """Write API keys into config.toml (created from the example if missing), keeping everything else."""
    import re

    path = Path(path) if path else ROOT / "config.toml"
    if not path.exists():
        example = ROOT / "config.example.toml"
        path.write_text(example.read_text(encoding="utf8") if example.exists() else "[keys]\n", encoding="utf8")
    text = path.read_text(encoding="utf8")
    if "[keys]" not in text:
        text = "[keys]\n" + text
    for name, value in keys.items():
        value = value.replace("\\", "").replace('"', "")
        line = f'{name} = "{value}"'
        pattern = re.compile(rf'^{re.escape(name)}\s*=\s*"[^"\n]*"', re.M)
        if pattern.search(text):
            text = pattern.sub(lambda m: line, text, count=1)
        else:
            text = text.replace("[keys]", f"[keys]\n{line}", 1)
    path.write_text(text, encoding="utf8")
    return path


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.toml"
    data = copy.deepcopy(DEFAULTS)  # callers change settings in place (e.g. the scan budget)
    if path.exists():
        with open(path, "rb") as fh:
            data = _merge(data, tomllib.load(fh))
    return Config(raw=data)
