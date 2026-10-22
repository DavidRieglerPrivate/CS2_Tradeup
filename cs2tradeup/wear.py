"""Wear tiers and float helpers."""
from __future__ import annotations

import struct

# (short code, full name, lower bound inclusive, upper bound exclusive)
WEARS = [
    ("FN", "Factory New", 0.00, 0.07),
    ("MW", "Minimal Wear", 0.07, 0.15),
    ("FT", "Field-Tested", 0.15, 0.38),
    ("WW", "Well-Worn", 0.38, 0.45),
    ("BS", "Battle-Scarred", 0.45, 1.00),
]
WEAR_BY_CODE = {w[0]: w for w in WEARS}
WEAR_BY_NAME = {w[1]: w for w in WEARS}


def f32(x: float) -> float:
    """Round a Python float to the nearest 32-bit float, the precision CS2 stores wear in."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def wear_of(fl: float) -> str:
    """Short wear code (FN/MW/FT/WW/BS) for a float value."""
    for code, _, lo, hi in WEARS:
        if fl < hi:
            return code
    return "BS"


def wear_name(code: str) -> str:
    return WEAR_BY_CODE[code][1]


def wear_upper(code: str) -> float:
    """Exclusive upper float bound of a wear tier (1.0 for BS)."""
    return WEAR_BY_CODE[code][3]


def possible_wears(min_float: float, max_float: float) -> list[str]:
    """Wear tiers a skin with this float range can actually exist in."""
    return [c for c, _, lo, hi in WEARS if min_float < hi and max_float > lo]


def parse_target(target: str, margin: float = 0.0) -> float:
    """Turn a target into the maximum allowed output float (inclusive).

    'MW' / 'Minimal Wear' means "strictly below 0.15", so the safety margin is subtracted.
    An explicit number such as '0.149999' is taken as-is (inclusive).
    """
    t = target.strip().upper()
    for code, name, lo, hi in WEARS:
        if t in (code, name.upper(), name.upper().replace("-", " ")):
            return 1.0 if code == "BS" else hi - margin
    try:
        val = float(t.replace(",", "."))
    except ValueError:
        raise ValueError(f"unknown condition '{target}': use FN, MW, FT, WW, BS or a maximum float "
                         "like 0.149999") from None
    if not 0.0 < val <= 1.0:
        raise ValueError(f"target float must be in (0, 1], got {val}")
    return val
