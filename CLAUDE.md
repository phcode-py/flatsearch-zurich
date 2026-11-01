# flatfind

Agent workspace for finding a rental flat in Zurich. The loop:
1. **scrape** new listings (flatfox.ch, homegate.ch) ← implemented
2. evaluate against the criteria ← structured scoring implemented; LLM fact extraction next
3. contact / fill visit form
4. escalate to the human when needed

## Commands

```sh
uv run flatfind scrape [--source flatfox|homegate] [--json]   # fetch new listings, store them, print new matches
uv run flatfind score [--id ID] [--all] [--limit N] [--no-geo]  # evaluate new listings (geo lookups cached)
uv run flatfind rank [--limit 20] [--json]                       # best scored listings + their unknowns
uv run flatfind set-fact <id> <variable>[:<key>] <json>          # settle an unknown from the description, rescores
uv run flatfind list [--status new] [--limit N] [--json]
uv run flatfind show <id> [--raw]                            # id = flatfox:<pk> | homegate:<id>
uv run flatfind stats
uv run pytest
```

Config: `config.toml` (per-source + geo settings). Criteria: `private/criteria.json` (gitignored, personal; schema
in `criteria.example.json`). DB: `data/flatfind.db`, OSM POI cache: `data/pois.json` (both gitignored).

## Criteria and scoring

`private/criteria.json` is a list of criteria, each on one variable: `category`, `price`, `rooms`,
`move_in_date`, `temporary`, `has_dishwasher`, `has_washingmachine`, `max_travel_time_sbb`,
`max_next_supermarket`, `free_requirement`, `is_dachgeschoss`, `n_lively`. A variable may appear twice
(e.g. price as required cap and as weighted preference).

- `aggregation: required` → hard filter. Applied at scrape time (status `filtered`) except geo variables,
  and pushed to sources via `SearchQuery` where the site supports it.
- `aggregation: weighted` + integer `weight` → score = Σ weight·reward / Σ weight, 0 if any required fails.
- Numeric `type`: `max` (ideal ≤ … ≤ limit, less better), `min` (more better), `tolerance` (`range`, midpoint
  best). Rewards are linear. Booleans use `value`, category `one_of`, free_requirement `items`.
- Unknown facts pass required checks and get `unknown_score` (0.5) when weighted. `rank` / `show` list them
  as e.g. `has_dishwasher`, `free_requirement:parquet floor`; resolve them from the description with
  `set-fact` (e.g. `set-fact flatfox:123 "free_requirement:parquet floor" true`).

Fact layers (later wins): structured listing data (`facts.py`) → geo lookups (`geo.py`) → `set-fact` overrides.

## Layout

- `src/flatfind/sources/` — one module per site implementing `sources/base.py`; register in `sources/__init__.py`.
- `models.py` — normalized `Listing`; `raw` keeps the full source payload.
- `store.py` — listings table with a `status` column (`new`, `filtered`, then owned by later steps).
- `sources/base.py` — the source interface (`NAME`, `scrape`, `parse`), `SearchQuery`, canonical feature names.
- `criteria.py` — criteria validation, scoring, `SearchQuery` derivation.
- `facts.py` / `geo.py` — criterion values from listing data / from SBB timetable + OpenStreetMap.

## Source notes

- **flatfox**: public JSON API. `/api/v1/pin/` (bbox, max 1000 → recursive quadrant split) for ids, then batched `/api/v1/public-listing/?pk=…`. Visit requests go via `contact_url` (`/listing/<pk>/submit/`).
- **homegate**: search pages embed `window.__INITIAL_STATE__`; sorted `o=dateCreated-desc`, paged with `ep=N`. Detail pages are behind a Cloudflare challenge — don't scrape them and don't try to bypass it. So `available_from` and `lister` are unknown for homegate; the description often states them. URL filter params are unreliable, so filtering happens locally.
- **geo**: travel times from transport.opendata.ch (address coordinates → station, weekday 08:00, incl.
  walking), cached per coordinate/station. Supermarkets, cafes, bars, pubs, cinemas, parks from OpenStreetMap
  via Overpass, fetched once for the whole area and cached for 7 days; walking time = straight line × 1.3 at
  80 m/min. Public Overpass servers are often overloaded, so several mirrors are tried; if all fail those facts
  stay unknown for the run.
