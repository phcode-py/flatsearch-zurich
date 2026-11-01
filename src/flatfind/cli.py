"""flatfind CLI.

  flatfind scrape [--source flatfox|homegate] [--json]
  flatfind score [--id ID ...] [--all] [--limit N] [--no-geo]
  flatfind rank [--limit 20] [--json]
  flatfind set-fact <id> <name>[:<key>] <json value>
  flatfind list [--status new] [--limit 50] [--json]
  flatfind show <id> [--raw]
  flatfind stats
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import tomllib
from pathlib import Path

from flatfind import criteria as crit
from flatfind import facts as factlib
from flatfind.geo import Geo, RateLimited
from flatfind.http import BlockedError, make_client
from flatfind.models import Listing
from flatfind.sources import SOURCES
from flatfind.store import Store

log = logging.getLogger("flatfind")


def _summary(l: Listing, status: str | None = None) -> str:
    rooms = f"{l.rooms:g} Zi" if l.rooms else "? Zi"
    area = f"{l.living_space_m2:g} m²" if l.living_space_m2 else "? m²"
    rent = f"CHF {l.rent_gross}" if l.rent_gross else "CHF ?"
    where = " ".join(x for x in (l.zipcode, l.city) if x)
    st = f"[{status}] " if status else ""
    return f"{st}{l.id:<20} {rooms:>7} {area:>7} {rent:>10}  {where:<14} {l.title[:60]}"


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def cmd_scrape(args, cfg, store: Store) -> int:
    criteria = crit.load(args.criteria)
    query = crit.search_query(criteria)
    new: list[Listing] = []
    failed = False
    with make_client() as client:
        for name, source in SOURCES.items():
            settings = cfg.get(name, {})
            if args.source and args.source != name or not settings.get("enabled", True):
                continue
            n_new = n_filtered = 0
            try:
                for listing in source.scrape(client, settings, query, store.known_ids(name)):
                    # Required criteria that need no lookups act as hard filters right away.
                    ev = crit.evaluate(criteria, factlib.from_listing(listing), skip=crit.GEO)
                    if store.add(listing, "new" if ev.passed else "filtered"):
                        if ev.passed:
                            n_new += 1
                            new.append(listing)
                        else:
                            n_filtered += 1
                            log.debug("filtered %s: %s", listing.id, ev.failures)
            except BlockedError as e:
                log.error("%s: blocked by bot protection (%s); skipping this run", name, e)
                failed = True
            except Exception:
                log.exception("%s: scrape failed", name)
                failed = True
            log.info("%s: %d new matching, %d filtered out", name, n_new, n_filtered)
    for l in new:
        print(_dump(l.to_dict()) if args.json else _summary(l))
    return 1 if failed else 0


def _geo_missing(geo_facts: dict, criteria: crit.Criteria) -> bool:
    wanted = criteria.variables() & crit.GEO
    return (
        any(k not in geo_facts for k in wanted)
        or bool(set(criteria.stations()) - set(geo_facts.get("max_travel_time_sbb", {})))
        or bool({str(m) for m in criteria.lively_walk_minutes()} - set(geo_facts.get("n_lively", {})))
    )


def _score_one(listing_id: str, criteria: crit.Criteria, store: Store, geo: Geo | None) -> crit.Evaluation:
    stored, _ = store.get(listing_id)
    listing = SOURCES[stored.source].parse(stored.raw)  # re-normalize with the current parser
    store.update_data(listing)
    extra = store.get_extra(listing_id)
    base, overrides, geo_facts = factlib.from_listing(listing), extra["facts"], extra["geo"] or {}

    # Lookups are slow and rate-limited: only for listings the cheap required checks let through.
    cheap = crit.evaluate(criteria, factlib.merge(base, overrides), skip=crit.GEO)
    if cheap.passed and geo and listing.lat is not None and listing.lon is not None \
            and _geo_missing(geo_facts, criteria):
        geo_facts = geo.facts(listing.lat, listing.lon, criteria.stations(),
                              criteria.lively_walk_minutes(), "max_next_supermarket" in criteria.variables())
        store.save_geo(listing_id, geo_facts)

    ev = crit.evaluate(criteria, factlib.merge(base, geo_facts, overrides))
    store.save_evaluation(listing_id, ev.to_dict(), "scored" if ev.passed else "rejected")
    return ev


def cmd_score(args, cfg, store: Store) -> int:
    criteria = crit.load(args.criteria)
    ids = args.id or store.ids(["new", "scored", "rejected"] if args.all else ["new"])
    ids = ids[: args.limit] if args.limit else ids
    with make_client() as client:
        geo = None if args.no_geo else Geo(client, store, _geo_settings(cfg), Path(args.db).parent / "pois.json")
        n_ok = n_rej = 0
        for i, lid in enumerate(ids, 1):
            try:
                ev = _score_one(lid, criteria, store, geo)
            except RateLimited as e:
                log.error("%s; stopping. %d listings left unscored, rerun later.", e, len(ids) - i + 1)
                return 1
            n_ok += ev.passed
            n_rej += not ev.passed
            if i % 25 == 0:
                log.info("scored %d/%d", i, len(ids))
    log.info("scored %d listings: %d passed, %d rejected by required criteria", len(ids), n_ok, n_rej)
    return 0


def _geo_settings(cfg) -> dict:
    """[geo] from config.toml; the POI area defaults to the flatfox box plus a margin."""
    geo = dict(cfg.get("geo", {}))
    box = cfg.get("flatfox", {})
    pad = geo.get("margin_deg", 0.02)
    for k, sign in (("west", -1), ("east", 1), ("south", -1), ("north", 1)):
        if k not in geo and k in box:
            geo[k] = box[k] + sign * pad
    return geo


def cmd_rank(args, cfg, store: Store) -> int:
    for l, status, score, ev in store.ranked(args.limit, args.include_rejected):
        if args.json:
            print(_dump({**l.to_dict(), "status": status, "score": score, "unknowns": ev["unknowns"]}))
        else:
            unk = f"  ({len(ev['unknowns'])} unknown)" if ev["unknowns"] else ""
            print(f"{score:5.2f}  {_summary(l, status if status == 'rejected' else None)}{unk}")
    return 0


def cmd_set_fact(args, cfg, store: Store) -> int:
    if not store.get(args.id):
        print(f"no listing {args.id}", file=sys.stderr)
        return 1
    name, _, key = args.name.partition(":")
    if name not in crit.VARIABLES:
        print(f"unknown variable {name}; expected one of {sorted(crit.VARIABLES)}", file=sys.stderr)
        return 1
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    if key and name not in ("free_requirement", "max_travel_time_sbb", "n_lively"):
        print(f"{name} takes no :key", file=sys.stderr)
        return 1
    try:
        value = crit.coerce_fact(name, value)
    except crit.CriteriaError as e:
        print(e, file=sys.stderr)
        return 1
    store.set_fact(args.id, name, value, key or None)
    ev = _score_one(args.id, crit.load(args.criteria), store, None)
    print(f"{args.id}: score {ev.score:.2f}, unknown: {', '.join(ev.unknowns) or '-'}")
    return 0


def cmd_list(args, cfg, store: Store) -> int:
    for l, status in store.list(args.status, args.limit):
        print(_dump({**l.to_dict(), "status": status}) if args.json else _summary(l, status))
    return 0


def cmd_show(args, cfg, store: Store) -> int:
    hit = store.get(args.id)
    if not hit:
        print(f"no listing {args.id}", file=sys.stderr)
        return 1
    l, status = hit
    out = {**l.to_dict(include_raw=args.raw), "status": status, **store.get_extra(args.id)}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_stats(args, cfg, store: Store) -> int:
    for (source, status), n in sorted(store.counts().items()):
        print(f"{source:<10} {status:<10} {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flatfind")
    p.add_argument("--config", default="config.toml")
    p.add_argument("--criteria", default="private/criteria.json")
    p.add_argument("--db", default="data/flatfind.db")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape", help="fetch new listings from all sources")
    s.add_argument("--source", choices=list(SOURCES))
    s.add_argument("--json", action="store_true", help="print new listings as JSON lines")
    s.set_defaults(func=cmd_scrape)

    s = sub.add_parser("score", help="evaluate listings against the criteria (incl. geo lookups)")
    s.add_argument("--id", action="append", help="score only this listing (repeatable)")
    s.add_argument("--all", action="store_true", help="rescore already scored/rejected listings too")
    s.add_argument("--limit", type=int)
    s.add_argument("--no-geo", action="store_true", help="skip travel-time / POI lookups")
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("rank", help="best scored listings")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--include-rejected", action="store_true", help="also list rejected listings, at the bottom")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_rank)

    s = sub.add_parser("set-fact", help="override a fact (e.g. from the description) and rescore")
    s.add_argument("id")
    s.add_argument("name", help="variable, or variable:key for free_requirement / max_travel_time_sbb")
    s.add_argument("value", help="JSON value: true, false, 3.5, \"2027-01-01\"")
    s.set_defaults(func=cmd_set_fact)

    s = sub.add_parser("list", help="list stored listings")
    s.add_argument("--status")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="show one listing in full")
    s.add_argument("id", help="e.g. flatfox:86504704 or homegate:4003594322")
    s.add_argument("--raw", action="store_true", help="include the raw source payload")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("stats", help="listing counts per source and status")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # behave when piped into head/grep
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = tomllib.loads(Path(args.config).read_text()) if Path(args.config).exists() else {}
    try:
        return args.func(args, cfg, Store(args.db))
    except (crit.CriteriaError, FileNotFoundError) as e:
        print(f"criteria: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
