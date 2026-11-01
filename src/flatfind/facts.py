"""Turn a normalized Listing into criterion values ("facts").

Facts come from three layers, later ones winning:
  1. structured listing data (this module)
  2. geo lookups (geo.py): travel times, supermarket, lively places
  3. manual / LLM overrides stored per listing (`flatfind set-fact`)
None means unknown. Absent features are unknown, not False: listers often
just don't tick the boxes, so the description has to settle it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from flatfind.models import Listing


def _move_in(v: str | None, today: date) -> date | None:
    if v == "immediately":
        return today
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None  # "by agreement" etc.


def from_listing(l: Listing, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    features = set(l.features)
    return {
        "category": l.category,
        "price": l.rent_gross if l.rent_gross is not None else l.rent_net,
        "rooms": l.rooms,
        "move_in_date": _move_in(l.available_from, today),
        "temporary": l.is_temporary,
        "has_dishwasher": True if "dishwasher" in features else None,
        "has_washingmachine": True if "washing_machine" in features else None,
        "is_dachgeschoss": l.is_top_floor,
    }


def merge(*layers: dict[str, Any]) -> dict[str, Any]:
    """Later layers override earlier ones; dict-valued facts merge per key; None never overrides."""
    out: dict[str, Any] = {}
    for layer in layers:
        for k, v in layer.items():
            if v is None:
                continue
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = {**out[k], **{kk: vv for kk, vv in v.items() if vv is not None}}
            else:
                out[k] = v
    return out
