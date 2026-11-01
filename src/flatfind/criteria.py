"""User criteria (private/criteria.json): validation, scoring, search-query derivation.

Each criterion targets one variable and is either
  - required: a hard filter (pass/fail), or
  - weighted: contributes `weight * reward` (reward in [0, 1]) to the score.

Final score = sum(weight * reward) / sum(weight), forced to 0 if any required
criterion failed. Numeric rewards are linear:
  type=max        1 at/below `ideal`, 0 at/above `limit`   (less is better)
  type=min        1 at/above `ideal`, 0 at/below `limit`   (more is better)
  type=tolerance  1 at the midpoint of `range`, 0 at its edges
For required criteria only `limit` / `range` matter. Unknown values pass
required checks and get `unknown_score` when weighted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from flatfind.sources.base import SearchQuery

NUMERIC = {"price", "rooms", "move_in_date", "max_travel_time_sbb", "max_next_supermarket", "n_lively"}
BOOLEAN = {"temporary", "has_dishwasher", "has_washingmachine", "is_dachgeschoss"}
VARIABLES = NUMERIC | BOOLEAN | {"category", "free_requirement"}
# Need geo lookups (slow, rate-limited) -> not available at scrape time.
GEO = {"max_travel_time_sbb", "max_next_supermarket", "n_lively"}
DEFAULT_TYPE = {"price": "max", "max_travel_time_sbb": "max", "max_next_supermarket": "max", "n_lively": "min"}


class CriteriaError(ValueError):
    pass


@dataclass
class Criterion:
    variable: str
    aggregation: str  # required | weighted
    weight: int = 0
    type: str | None = None  # max | min | tolerance (numeric variables)
    ideal: float | None = None
    limit: float | None = None
    range: tuple[float, float] | None = None
    value: Any = None  # boolean variables
    one_of: list[str] = field(default_factory=list)  # category
    items: list[str] = field(default_factory=list)  # free_requirement
    stations: list[str] = field(default_factory=list)  # max_travel_time_sbb
    walk_minutes: float = 10  # n_lively

    @property
    def label(self) -> str:
        return f"{self.variable} ({self.aggregation}{f', w={self.weight}' if self.weight else ''})"


@dataclass
class Criteria:
    criteria: list[Criterion]
    unknown_score: float = 0.5

    def variables(self) -> set[str]:
        return {c.variable for c in self.criteria}

    def stations(self) -> list[str]:
        return sorted({s for c in self.criteria for s in c.stations})

    def lively_walk_minutes(self) -> list[float]:
        return sorted({c.walk_minutes for c in self.criteria if c.variable == "n_lively"})


def _num(v: Any) -> float:
    """Numbers stay numbers; ISO dates become day ordinals so they interpolate."""
    if isinstance(v, date):
        return float(v.toordinal())
    if isinstance(v, str):
        return float(date.fromisoformat(v).toordinal())
    return float(v)


def _parse(i: int, d: dict[str, Any]) -> Criterion:
    where = f"criteria[{i}] ({d.get('variable')})"
    var = d.get("variable")
    if var not in VARIABLES:
        raise CriteriaError(f"{where}: unknown variable; expected one of {sorted(VARIABLES)}")
    agg = d.get("aggregation")
    if agg not in ("required", "weighted"):
        raise CriteriaError(f"{where}: aggregation must be 'required' or 'weighted'")
    weight = d.get("weight", 0)
    if agg == "weighted" and (not isinstance(weight, int) or weight <= 0):
        raise CriteriaError(f"{where}: weighted criteria need a positive integer weight")
    known = {"variable", "aggregation", "weight", "type", "ideal", "limit", "range", "value",
             "one_of", "items", "stations", "walk_minutes", "comment"}
    if extra := set(d) - known:
        raise CriteriaError(f"{where}: unknown keys {sorted(extra)}")

    c = Criterion(variable=var, aggregation=agg, weight=weight if agg == "weighted" else 0)
    if var in NUMERIC:
        c.type = d.get("type", DEFAULT_TYPE.get(var))
        if c.type not in ("max", "min", "tolerance"):
            raise CriteriaError(f"{where}: type must be max, min or tolerance")
        if c.type == "tolerance":
            r = d.get("range")
            if not (isinstance(r, list) and len(r) == 2):
                raise CriteriaError(f"{where}: tolerance needs range: [low, high]")
            c.range = (_num(r[0]), _num(r[1]))
            if c.range[0] >= c.range[1]:
                raise CriteriaError(f"{where}: range must be increasing")
        else:
            if d.get("limit") is None:
                raise CriteriaError(f"{where}: {c.type} needs a limit")
            c.limit = _num(d["limit"])
            if agg == "weighted":
                if d.get("ideal") is None:
                    raise CriteriaError(f"{where}: weighted {c.type} needs an ideal")
                c.ideal = _num(d["ideal"])
                if (c.ideal >= c.limit) if c.type == "max" else (c.ideal <= c.limit):
                    side = "below" if c.type == "max" else "above"
                    raise CriteriaError(f"{where}: ideal must be {side} limit for type {c.type}")
        if var == "max_travel_time_sbb":
            c.stations = list(d.get("stations") or [])
            if not c.stations:
                raise CriteriaError(f"{where}: needs a non-empty stations list")
        if var == "n_lively":
            c.walk_minutes = float(d.get("walk_minutes", 10))
    elif var in BOOLEAN:
        if not isinstance(d.get("value"), bool):
            raise CriteriaError(f"{where}: needs value: true|false")
        c.value = d["value"]
    elif var == "category":
        c.one_of = list(d.get("one_of") or [])
        if not c.one_of:
            raise CriteriaError(f"{where}: needs a non-empty one_of list")
    elif var == "free_requirement":
        c.items = list(d.get("items") or [])
        if not c.items:
            raise CriteriaError(f"{where}: needs a non-empty items list")
    return c


def load(path: str | Path) -> Criteria:
    data = json.loads(Path(path).read_text())
    if not isinstance(data.get("criteria"), list):
        raise CriteriaError("top-level 'criteria' must be a list")
    return Criteria(
        criteria=[_parse(i, d) for i, d in enumerate(data["criteria"])],
        unknown_score=float(data.get("unknown_score", 0.5)),
    )


# ---- fact types ------------------------------------------------------------

TRUE_WORDS = {"true", "yes", "ja", "y", "1"}
FALSE_WORDS = {"false", "no", "nein", "n", "0"}


def _valid(variable: str, v: Any) -> Any:
    """A single (part) value if it has the right type for `variable`, else None (= unknown)."""
    if v is None:
        return None
    if variable == "move_in_date":
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(v) if isinstance(v, str) else None
        except ValueError:
            return None
    if variable in NUMERIC:
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if variable in BOOLEAN or variable == "free_requirement":
        return v if isinstance(v, bool) else None
    if variable == "category":
        return v if isinstance(v, str) else None
    return v


def coerce_fact(variable: str, raw: Any) -> Any:
    """Strictly convert a manual / LLM fact (often a string) to the variable's type."""
    v = raw.strip().lower() if isinstance(raw, str) else raw
    if variable in BOOLEAN or variable == "free_requirement":
        if isinstance(v, bool):
            return v
        if v in TRUE_WORDS | FALSE_WORDS:
            return v in TRUE_WORDS
    elif variable == "move_in_date":
        if isinstance(v, str) and _valid(variable, v):
            return v
    elif variable in NUMERIC:
        try:
            if not isinstance(v, bool):
                return float(v) if "." in str(v) else int(v)
        except (TypeError, ValueError):
            pass
    elif variable == "category" and isinstance(v, str):
        return v
    raise CriteriaError(f"{raw!r} is not a valid value for {variable}")


# ---- scoring ---------------------------------------------------------------


def _reward(c: Criterion, x: float) -> float:
    if c.type == "tolerance":
        lo, hi = c.range
        mid, half = (lo + hi) / 2, (hi - lo) / 2
        return max(0.0, 1 - abs(x - mid) / half)
    if c.type == "max":
        return min(1.0, max(0.0, (c.limit - x) / (c.limit - c.ideal)))
    return min(1.0, max(0.0, (x - c.limit) / (c.ideal - c.limit)))


def _passes(c: Criterion, x: float) -> bool:
    if c.type == "tolerance":
        return c.range[0] <= x <= c.range[1]
    return x <= c.limit if c.type == "max" else x >= c.limit


def _parts(c: Criterion, facts: dict[str, Any]) -> list[tuple[str, Any]]:
    """Split a criterion into (label, value) parts; None value = unknown."""
    v = facts.get(c.variable)
    if c.variable == "max_travel_time_sbb":
        return [(s, (v or {}).get(s)) for s in c.stations]
    if c.variable == "free_requirement":
        return [(it, (v or {}).get(it)) for it in c.items]
    if c.variable == "n_lively":
        return [("", (v or {}).get(str(c.walk_minutes)) if isinstance(v, dict) else v)]
    return [("", v)]


@dataclass
class Result:
    criterion: str
    passed: bool | None = None  # required only
    reward: float | None = None  # weighted only
    value: Any = None
    unknown: list[str] = field(default_factory=list)


@dataclass
class Evaluation:
    score: float
    passed: bool
    results: list[Result]

    @property
    def unknowns(self) -> list[str]:
        return [f"{r.criterion.split(' ')[0]}{':' + u if u else ''}" for r in self.results for u in r.unknown]

    @property
    def failures(self) -> list[str]:
        return [f"{r.criterion} = {r.value}" for r in self.results if r.passed is False]

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "passed": self.passed,
            "failures": self.failures,
            "unknowns": self.unknowns,
            "results": [r.__dict__ for r in self.results],
        }


def _check(c: Criterion, value: Any) -> bool:
    if c.variable in NUMERIC:
        return _passes(c, _num(value))
    if c.variable in BOOLEAN or c.variable == "free_requirement":
        return value == (c.value if c.variable in BOOLEAN else True)
    if c.variable == "category":
        return value in c.one_of
    raise AssertionError(c.variable)


def _score_part(c: Criterion, value: Any) -> float:
    if c.variable in NUMERIC:
        return _reward(c, _num(value))
    return 1.0 if _check(c, value) else 0.0


def evaluate(criteria: Criteria, facts: dict[str, Any], skip: set[str] = frozenset()) -> Evaluation:
    results: list[Result] = []
    passed = True
    total = weights = 0.0
    for c in criteria.criteria:
        if c.variable in skip:
            continue
        parts = [(label, _valid(c.variable, v)) for label, v in _parts(c, facts)]
        unknown = [label for label, v in parts if v is None]
        known = [(label, v) for label, v in parts if v is not None]
        value = facts.get(c.variable)
        if c.aggregation == "required":
            ok = all(_check(c, v) for _, v in known)
            passed &= ok
            results.append(Result(c.label, passed=ok, value=value, unknown=unknown))
        else:
            rewards = [_score_part(c, v) for _, v in known] + [criteria.unknown_score] * len(unknown)
            reward = sum(rewards) / len(rewards)
            total += c.weight * reward
            weights += c.weight
            results.append(Result(c.label, reward=round(reward, 4), value=value, unknown=unknown))
    score = (total / weights if weights else 1.0) if passed else 0.0
    return Evaluation(score=score, passed=passed, results=results)


def search_query(criteria: Criteria) -> SearchQuery:
    """What the required criteria allow sources to filter server-side."""
    q = SearchQuery()
    for c in criteria.criteria:
        if c.aggregation != "required":
            continue
        if c.variable == "category":
            q.categories = c.one_of
        elif c.variable == "temporary" and c.value is False:
            q.exclude_temporary = True
        elif c.variable in ("price", "rooms"):
            lo, hi = c.range if c.type == "tolerance" else (
                (None, c.limit) if c.type == "max" else (c.limit, None))
            if lo is not None:
                setattr(q, f"min_{c.variable}", lo)
            if hi is not None:
                setattr(q, f"max_{c.variable}", hi)
    return q
