"""homegate.ch via the search result pages.

Search pages embed the full result set (incl. descriptions and images) as JSON
in `window.__INITIAL_STATE__`. Listing detail pages sit behind a bot challenge,
so we deliberately rely on search pages only. Results are sorted newest-first,
so incremental runs stop at the first page with nothing new.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx

from flatfind.http import get
from flatfind.models import Listing
from flatfind.sources.base import SearchQuery

log = logging.getLogger(__name__)

NAME = "homegate"
BASE = "https://www.homegate.ch"
STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
TAG_RE = re.compile(r"<[^>]+>")
# homegate has no structured "temporary" flag; listers put it in the title.
TEMPORARY_RE = re.compile(r"(?<!un)befristet|temporär|temporary|untermiete|sublet|zwischenmiete", re.I)
TOP_FLOOR_CATEGORIES = {"ATTIC_FLAT", "ROOF_FLAT"}


def extract_state(page: str) -> dict[str, Any]:
    m = STATE_RE.search(page)
    if not m:
        raise ValueError("homegate: __INITIAL_STATE__ not found (page layout changed?)")
    # The blob is a JS object literal; `undefined` is the only non-JSON token seen.
    state, _ = json.JSONDecoder().raw_decode(page[m.end():].replace(":undefined", ":null"))
    return state


def search_result(state: dict[str, Any]) -> dict[str, Any]:
    return state["resultList"]["search"]["fullSearch"]["result"]


def _category(cats: list[str]) -> str:
    cats = set(cats)
    if cats & {"PARKING_SPACE", "PARKING", "GARAGE", "UNDERGROUND_SLOT", "SINGLE_GARAGE", "DOUBLE_GARAGE", "OPEN_SLOT", "COVERED_SLOT"}:
        return "parking"
    if cats & {"ROOM", "SINGLE_ROOM"}:
        return "room"
    if cats & {"HOUSE", "SINGLE_HOUSE", "ROW_HOUSE", "VILLA", "BIFAMILIAR_HOUSE", "TERRACE_HOUSE", "FARM_HOUSE", "CHALET"}:
        return "house"
    if cats & {"APARTMENT", "FLAT", "DUPLEX", "ATTIC_FLAT", "ROOF_FLAT", "STUDIO", "LOFT", "MAISONETTE", "TERRACE_FLAT", "FURNISHED_FLAT", "BACHELOR_FLAT"}:
        return "apartment"
    return "other"


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<(br|/p|/ul|/ol)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = html.unescape(TAG_RE.sub("", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _top_floor(categories: list[str], ch: dict[str, Any]) -> bool | None:
    if TOP_FLOOR_CATEGORIES & set(categories):
        return True
    floor, floors = ch.get("floor"), ch.get("numberOfFloors")
    if floor is None or not floors:
        return None
    # Unclear whether numberOfFloors counts the ground floor -> only decide clear cases.
    if floor >= floors:
        return True
    return False if floor < floors - 1 else None


def parse(item: dict[str, Any]) -> Listing:
    l = item["listing"]
    addr = l.get("address") or {}
    geo = addr.get("geoCoordinates") or {}
    ch = l.get("characteristics") or {}
    rent = (l.get("prices") or {}).get("rent") or {}
    loc = l.get("localization") or {}
    # Prefer the lister's own language, then English/German.
    langs = [loc.get("primary"), "en", "de", "fr", "it"]
    text = next((loc[x]["text"] for x in langs if x in loc and loc[x].get("text")), {})
    attachments = next((loc[x].get("attachments") for x in langs if x in loc and loc[x].get("attachments")), [])
    gross = rent.get("gross")
    net = rent.get("net")
    extra = rent.get("extra")
    features = sorted(
        _snake(re.sub(r"^(has|is|are)", "", k))
        for k, v in ch.items()
        if v is True and re.match(r"^(has|is|are)[A-Z]", k)
    )
    description = _clean(text.get("description"))
    if text.get("situation"):
        description += "\n\nLage / Situation:\n" + _clean(text["situation"])
    lid = str(item.get("id") or l["id"])
    title = _clean(text.get("title"))
    return Listing(
        source="homegate",
        source_id=lid,
        url=f"{BASE}/rent/{lid}",
        title=title,
        description=description,
        category=_category(l.get("categories") or []),
        rooms=ch.get("numberOfRooms"),
        living_space_m2=ch.get("livingSpace"),
        floor=ch.get("floor"),
        rent_gross=gross if gross is not None else (net + extra if net is not None and extra is not None else None),
        rent_net=net,
        rent_charges=extra,
        street=addr.get("street"),
        zipcode=addr.get("postalCode"),
        city=addr.get("locality"),
        lat=geo.get("latitude"),
        lon=geo.get("longitude"),
        available_from=None,  # only on detail pages
        is_furnished=ch.get("isFurnished"),
        is_temporary=True if TEMPORARY_RE.search(title) else None,
        is_top_floor=_top_floor(l.get("categories") or [], ch),
        features=features,
        images=[a["url"] for a in attachments if a.get("type") == "IMAGE" and a.get("url")],
        published_at=(l.get("meta") or {}).get("createdAt"),
        lister=None,
        contact_url=f"{BASE}/rent/{lid}",
        raw=item,
    )


def scrape(client: httpx.Client, settings: dict[str, Any], query: SearchQuery, known: set[str]) -> Iterator[Listing]:
    # homegate's URL filter params proved unreliable, so `query` is applied locally only.
    url = f"{BASE}/rent/real-estate/{settings['location']}/matching-list"
    for page in range(1, settings.get("max_pages", 15) + 1):
        if page > 1:
            time.sleep(settings.get("request_delay_s", 1.5))
        params = {"o": "dateCreated-desc"} | ({"ep": page} if page > 1 else {})
        result = search_result(extract_state(get(client, url, params=params).text))
        items = result.get("listings") or []
        fresh = [it for it in items if str(it.get("id") or it["listing"]["id"]) not in known]
        log.info("homegate: page %d/%s, %d listings, %d new", page, result.get("pageCount"), len(items), len(fresh))
        for it in fresh:
            if it["listing"].get("offerType", "RENT") == "RENT":
                yield parse(it)
        if not fresh or not result.get("hasNextPage"):
            break
