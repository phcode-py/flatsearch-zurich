"""Location facts: public-transport travel times, nearest supermarket, lively places.

- Travel times: transport.opendata.ch (official SBB open-data timetable). It
  accepts the flat's coordinates as origin and includes the walk to the stop.
  Queried for a fixed weekday morning so results are comparable; cached.
- Supermarkets / cafes / bars / cinemas / parks: OpenStreetMap via Overpass,
  fetched once for the whole area and cached on disk, then computed locally.
  Walking times use straight-line distance * DETOUR at WALK_M_PER_MIN.
  Supermarket time is walking only; for the nearest shop, tram is rarely faster.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from flatfind.store import Store

log = logging.getLogger(__name__)

OPENDATA = "https://transport.opendata.ch/v1/connections"
# Public Overpass instances are often overloaded; tried in order.
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
WALK_M_PER_MIN = 80  # ~4.8 km/h
DETOUR = 1.3  # street network vs straight line
LIVELY = {"cafe", "bar", "pub", "cinema", "park"}
POI_MAX_AGE_S = 7 * 24 * 3600
# Open-data APIs: identify honestly (Overpass rejects browser-like agents with 406).
HEADERS = {"User-Agent": "flatfind/0.1 (personal flat search)"}


class RateLimited(RuntimeError):
    pass


def walk_minutes(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    meters = 2 * 6_371_000 * math.asin(math.sqrt(a))
    return meters * DETOUR / WALK_M_PER_MIN


def _duration_minutes(s: str) -> float:
    days, hms = s.split("d")
    h, m, sec = (int(x) for x in hms.split(":"))
    return int(days) * 1440 + h * 60 + m + sec / 60


def reference_departure(weekday: int = 1, at: str = "08:00") -> tuple[str, str]:
    """Next given weekday (0=Mon) from today, as (date, time) for the timetable query."""
    today = date.today()
    d = today + timedelta(days=(weekday - today.weekday()) % 7 or 7)
    return d.isoformat(), at


class Geo:
    def __init__(self, client: httpx.Client, store: Store, settings: dict[str, Any], poi_path: str | Path):
        self.client = client
        self.store = store
        self.settings = settings
        self.poi_path = Path(poi_path)
        self._pois: list[dict[str, Any]] | None = None  # [] = unavailable this run

    # ---- OpenStreetMap POIs -------------------------------------------------

    def pois(self) -> list[dict[str, Any]]:
        if self._pois is None:
            fresh = self.poi_path.exists() and time.time() - self.poi_path.stat().st_mtime < POI_MAX_AGE_S
            if not fresh:
                try:
                    pois = self._fetch_pois()
                except RuntimeError as e:
                    # Keep a stale cache if there is one, else POI facts stay unknown this run.
                    log.warning("geo: %s", e)
                    self._pois = json.loads(self.poi_path.read_text()) if self.poi_path.exists() else []
                    return self._pois
                self.poi_path.parent.mkdir(parents=True, exist_ok=True)
                self.poi_path.write_text(json.dumps(pois))
            self._pois = json.loads(self.poi_path.read_text())
        return self._pois

    def _fetch_pois(self) -> list[dict[str, Any]]:
        s, w, n, e = (self.settings[k] for k in ("south", "west", "north", "east"))
        box = f"({s},{w},{n},{e})"
        query = (
            f'[out:json][timeout:180];('
            f'nwr["shop"="supermarket"]{box};'
            f'nwr["amenity"~"^(cafe|bar|pub|cinema)$"]{box};'
            f'nwr["leisure"="park"]{box};'
            f');out tags center;'
        )
        for url in self.settings.get("overpass", OVERPASS):
            log.info("geo: fetching POIs from %s (can take a few minutes)", url)
            try:
                r = self.client.post(url, data={"data": query}, headers=HEADERS,
                                     timeout=httpx.Timeout(150, connect=15))
                r.raise_for_status()
                elements = r.json()["elements"]
                break
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.warning("geo: %s failed: %s", url, e)
        else:
            raise RuntimeError("no Overpass instance answered; supermarket / lively facts stay unknown")
        pois = []
        for el in elements:
            t = el.get("tags", {})
            kind = "supermarket" if t.get("shop") == "supermarket" else t.get("amenity") or t.get("leisure")
            lat, lon = (el["lat"], el["lon"]) if "lat" in el else (el["center"]["lat"], el["center"]["lon"])
            pois.append({"kind": kind, "name": t.get("name"), "lat": lat, "lon": lon})
        log.info("geo: %d POIs cached in %s", len(pois), self.poi_path)
        return pois

    def nearest_supermarket(self, lat: float, lon: float) -> tuple[float, str | None] | None:
        best = min(
            ((walk_minutes(lat, lon, p["lat"], p["lon"]), p["name"]) for p in self.pois() if p["kind"] == "supermarket"),
            default=None,
        )
        return (round(best[0], 1), best[1]) if best else None

    def lively_count(self, lat: float, lon: float, minutes: float) -> int:
        return sum(1 for p in self.pois() if p["kind"] in LIVELY and walk_minutes(lat, lon, p["lat"], p["lon"]) <= minutes)

    # ---- public transport ---------------------------------------------------

    def transit_minutes(self, lat: float, lon: float, station: str) -> float | None:
        day, at = reference_departure(self.settings.get("weekday", 1), self.settings.get("depart_at", "08:00"))
        key = f"transit:{lat:.4f},{lon:.4f}->{station}@{at}"
        if (hit := self.store.cache_get(key)) is not None:
            return hit["minutes"]
        time.sleep(self.settings.get("request_delay_s", 0.5))
        r = self.client.get(OPENDATA, params={"from": f"{lat:.6f},{lon:.6f}", "to": station, "date": day, "time": at, "limit": 4},
                            headers=HEADERS)
        if r.status_code == 429:
            raise RateLimited("transport.opendata.ch rate limit reached")
        r.raise_for_status()
        data = r.json()
        conns = data.get("connections") or []
        minutes = min((_duration_minutes(c["duration"]) for c in conns if c.get("duration")), default=None)
        resolved = (data.get("to") or {}).get("name")
        if resolved and resolved != station:
            log.warning("geo: station %r resolved to %r; use the exact name in criteria", station, resolved)
        self.store.cache_put(key, {"minutes": minutes, "resolved": resolved})
        return minutes

    # ---- all location facts for one listing ---------------------------------

    def facts(self, lat: float, lon: float, stations: list[str], lively_minutes: list[float],
              want_supermarket: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if want_supermarket and (sm := self.nearest_supermarket(lat, lon)):
            out["max_next_supermarket"] = sm[0]
            out["nearest_supermarket"] = sm[1]
        if lively_minutes and self.pois():
            out["n_lively"] = {str(m): self.lively_count(lat, lon, m) for m in lively_minutes}
        if stations:
            out["max_travel_time_sbb"] = {s: self.transit_minutes(lat, lon, s) for s in stations}
        return out
