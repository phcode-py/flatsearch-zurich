"""flatfox.ch via its public JSON API.

Discovery uses the map-pin endpoint (bounding box, capped at 1000 results per
query -> recursively split the box into quadrants when the cap is hit). Details
are fetched in batches from the public-listing endpoint, only for unseen ids.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterator
from typing import Any

import httpx

from flatfind.http import get
from flatfind.models import Listing
from flatfind.sources.base import SearchQuery

log = logging.getLogger(__name__)

NAME = "flatfox"
BASE = "https://flatfox.ch"
PIN_CAP = 1000
DETAIL_BATCH = 50

CATEGORY_TO_FLATFOX = {
    "apartment": "APARTMENT",
    "house": "HOUSE",
    "room": "SHARED",
    "parking": "PARK",
}
FLATFOX_TO_CATEGORY = {v: k for k, v in CATEGORY_TO_FLATFOX.items()}

MOVING_DATE = {"imm": "immediately", "agr": "by agreement"}
TOP_FLOOR_TYPES = {"ATTIC_FLAT", "ROOF_FLAT"}
FEATURE_NAMES = {
    "balconygarden": "balcony", "washingmachine": "washing_machine", "tumbler": "tumble_dryer",
    "lift": "elevator", "parquetflooring": "parquet_floor", "stonefloor": "stone_floor",
    "view": "nice_view", "parkingspace": "parking", "petsallowed": "pets_allowed",
    "accessiblewithwheelchair": "wheelchair_accessible", "minergie": "minergie_certified",
    "cable": "cable_tv", "broadbandinternet": "broadband", "raisedgroundfloor": "ground_floor_raised",
}


def _pin_params(query: SearchQuery) -> dict[str, Any]:
    p: dict[str, Any] = {"offer_type": "RENT", "max_count": PIN_CAP}
    if cats := [CATEGORY_TO_FLATFOX[c] for c in query.categories if c in CATEGORY_TO_FLATFOX]:
        p["object_category"] = cats
    for name in ("min_rooms", "max_rooms"):
        if (v := getattr(query, name)) is not None:
            p[name] = v
    # The API rejects non-integer prices (e.g. 4000.0). Round outwards so the
    # server-side filter never drops what the local check would accept.
    if query.min_price is not None:
        p["min_price"] = math.floor(query.min_price)
    if query.max_price is not None:
        p["max_price"] = math.ceil(query.max_price)
    return p


def discover_ids(
    client: httpx.Client, west: float, east: float, south: float, north: float,
    query: SearchQuery, depth: int = 0,
) -> set[int]:
    params = {**_pin_params(query), "west": west, "east": east, "south": south, "north": north}
    pins = get(client, f"{BASE}/api/v1/pin/", params=params).json()
    if len(pins) < PIN_CAP or depth >= 6:
        if depth >= 6:
            log.warning("flatfox: pin cap still hit at max depth; some listings may be missed")
        return {p["pk"] for p in pins}
    mx, my = (west + east) / 2, (south + north) / 2
    ids: set[int] = set()
    for w, e, s, n in [(west, mx, south, my), (mx, east, south, my), (west, mx, my, north), (mx, east, my, north)]:
        ids |= discover_ids(client, w, e, s, n, query, depth + 1)
    return ids


def fetch_details(client: httpx.Client, pks: list[int]) -> Iterator[dict[str, Any]]:
    for i in range(0, len(pks), DETAIL_BATCH):
        batch = pks[i : i + DETAIL_BATCH]
        data = get(
            client, f"{BASE}/api/v1/public-listing/",
            params={"pk": batch, "expand": ["images", "cover_image"], "limit": DETAIL_BATCH},
        ).json()
        yield from data["results"]


def _num(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse(d: dict[str, Any]) -> Listing:
    moving = d.get("moving_date_type")
    agency = d.get("agency") or {}
    images = [BASE + img["url"] for img in d.get("images") or [] if isinstance(img, dict) and img.get("url")]
    return Listing(
        source="flatfox",
        source_id=str(d["pk"]),
        url=BASE + d["url"],
        title=d.get("description_title") or d.get("public_title") or d.get("short_title") or "",
        description=d.get("description") or "",
        category=FLATFOX_TO_CATEGORY.get(d.get("object_category"), "other"),
        rooms=_num(d.get("number_of_rooms")),
        living_space_m2=_num(d.get("surface_living") or d.get("livingspace")),
        floor=d.get("floor"),
        rent_gross=d.get("rent_gross") or (d.get("price_display") if d.get("price_display_type") == "TOTAL" else None),
        rent_net=d.get("rent_net"),
        rent_charges=d.get("rent_charges"),
        street=d.get("street"),
        zipcode=str(d["zipcode"]) if d.get("zipcode") else None,
        city=d.get("city"),
        lat=d.get("latitude"),
        lon=d.get("longitude"),
        available_from=d.get("moving_date") if moving == "dat" else MOVING_DATE.get(moving),
        is_furnished=d.get("is_furnished"),
        is_temporary=d.get("is_temporary"),
        is_top_floor=True if d.get("object_type") in TOP_FLOOR_TYPES else None,
        features=[FEATURE_NAMES.get(a["name"], a["name"]) for a in d.get("attributes") or []],
        images=images,
        published_at=d.get("published"),
        lister=agency.get("name"),
        contact_url=BASE + d["submit_url"] if d.get("submit_url") else None,
        raw=d,
    )


def scrape(client: httpx.Client, settings: dict[str, Any], query: SearchQuery, known: set[str]) -> Iterator[Listing]:
    ids = discover_ids(client, settings["west"], settings["east"], settings["south"], settings["north"], query)
    new = sorted((pk for pk in ids if str(pk) not in known), reverse=True)
    log.info("flatfox: %d listings in area, %d new", len(ids), len(new))
    for d in fetch_details(client, new):
        if d.get("status") == "act" and d.get("offer_type") == "RENT":
            yield parse(d)
