"""The interface every listing source implements.

A source is a module (registered in `sources/__init__.py`) exposing:

    NAME: str
    def scrape(client, settings, query, known) -> Iterator[Listing]
        settings: the source's own section of config.toml (area, paging, ...)
        query:    SearchQuery derived from the *required* user criteria; apply
                  whatever the site can filter server-side, ignore the rest
                  (everything is re-checked locally afterwards)
        known:    source_ids already stored; skip fetching these
    def parse(raw: dict) -> Listing
        raw payload (as stored in Listing.raw) -> normalized Listing, so stored
        listings can be re-normalized after parser changes

Sources must normalize into the shared vocabulary of `Listing`: categories
(apartment|house|room|parking|other), feature names (FEATURES below), CHF
amounts, and `is_temporary` / `is_top_floor` where the site tells.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from flatfind.models import Listing

# Canonical feature names used in Listing.features.
FEATURES = {
    "balcony", "dishwasher", "washing_machine", "tumble_dryer", "elevator", "parquet_floor",
    "stone_floor", "nice_view", "fireplace", "garage", "parking", "pets_allowed",
    "wheelchair_accessible", "minergie_certified", "cable_tv", "broadband", "ground_floor_raised",
}


@dataclass
class SearchQuery:
    categories: list[str] = field(default_factory=list)
    min_price: float | None = None
    max_price: float | None = None
    min_rooms: float | None = None
    max_rooms: float | None = None
    exclude_temporary: bool = False


class Source(Protocol):
    NAME: str

    def scrape(
        self, client: httpx.Client, settings: dict[str, Any], query: SearchQuery, known: set[str]
    ) -> Iterator[Listing]: ...

    def parse(self, raw: dict[str, Any]) -> Listing: ...
