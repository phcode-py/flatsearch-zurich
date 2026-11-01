from datetime import date
from pathlib import Path

import pytest

from flatfind import criteria as crit
from flatfind.facts import from_listing, merge
from flatfind.models import Listing

ROOT = Path(__file__).parent.parent


def C(*entries, unknown_score=0.5):
    return crit.Criteria([crit._parse(i, e) for i, e in enumerate(entries)], unknown_score)


def score(criteria, **facts):
    return crit.evaluate(criteria, facts)


def test_example_file_is_valid():
    c = crit.load(ROOT / "criteria.example.json")
    assert crit.GEO <= c.variables()


def test_linear_rewards():
    mx = C({"variable": "price", "aggregation": "weighted", "weight": 1, "type": "max", "ideal": 2000, "limit": 4000})
    assert [score(mx, price=p).score for p in (1500, 2000, 3000, 4000, 5000)] == [1, 1, 0.5, 0, 0]
    mn = C({"variable": "n_lively", "aggregation": "weighted", "weight": 1, "type": "min", "ideal": 40, "limit": 0})
    assert score(mn, n_lively={"10.0": 10}).score == 0.25
    tol = C({"variable": "rooms", "aggregation": "weighted", "weight": 1, "type": "tolerance", "range": [2.5, 4.5]})
    assert [score(tol, rooms=r).score for r in (2, 2.5, 3, 3.5, 4.5)] == [0, 0, 0.5, 1, 0]


def test_dates_interpolate():
    c = C({"variable": "move_in_date", "aggregation": "weighted", "weight": 1, "type": "tolerance",
           "range": ["2027-01-01", "2027-01-31"]})
    assert score(c, move_in_date=date(2027, 1, 16)).score == 1
    assert score(c, move_in_date=date(2026, 12, 1)).score == 0


def test_weighted_sum_and_required_clip():
    c = C(
        {"variable": "price", "aggregation": "required", "type": "max", "limit": 4000},
        {"variable": "has_dishwasher", "aggregation": "weighted", "weight": 3, "value": True},
        {"variable": "is_dachgeschoss", "aggregation": "weighted", "weight": 1, "value": True},
    )
    assert score(c, price=3000, has_dishwasher=True, is_dachgeschoss=False).score == 0.75
    ev = score(c, price=4500, has_dishwasher=True, is_dachgeschoss=True)
    assert ev.score == 0 and not ev.passed and ev.failures


def test_unknowns():
    c = C(
        {"variable": "temporary", "aggregation": "required", "value": False},
        {"variable": "free_requirement", "aggregation": "weighted", "weight": 1, "items": ["parquet", "balcony"]},
        {"variable": "max_travel_time_sbb", "aggregation": "weighted", "weight": 1, "stations": ["A", "B"],
         "ideal": 10, "limit": 30},
    )
    ev = score(c, free_requirement={"parquet": True}, max_travel_time_sbb={"A": 10})
    assert ev.passed  # unknown passes required
    # free_requirement: (1 + 0.5)/2, travel: (1 + 0.5)/2
    assert ev.score == 0.75
    assert ev.unknowns == ["temporary", "free_requirement:balcony", "max_travel_time_sbb:B"]


def test_search_query():
    q = crit.search_query(C(
        {"variable": "category", "aggregation": "required", "one_of": ["apartment"]},
        {"variable": "price", "aggregation": "required", "type": "max", "limit": 3500},
        {"variable": "rooms", "aggregation": "required", "type": "tolerance", "range": [2.5, 4]},
        {"variable": "price", "aggregation": "weighted", "weight": 1, "type": "max", "ideal": 1, "limit": 2},
        {"variable": "temporary", "aggregation": "required", "value": False},
    ))
    assert (q.categories, q.max_price, q.min_price, q.min_rooms, q.max_rooms, q.exclude_temporary) == \
        (["apartment"], 3500, None, 2.5, 4, True)


@pytest.mark.parametrize("entry", [
    {"variable": "nope", "aggregation": "required"},
    {"variable": "price", "aggregation": "weighted", "type": "max", "ideal": 1, "limit": 2},  # no weight
    {"variable": "price", "aggregation": "weighted", "weight": 1, "type": "max", "ideal": 3, "limit": 2},
    {"variable": "rooms", "aggregation": "required", "type": "tolerance", "range": [4, 2]},
    {"variable": "has_dishwasher", "aggregation": "required", "value": "yes"},
    {"variable": "max_travel_time_sbb", "aggregation": "required", "limit": 20},  # no stations
    {"variable": "price", "aggregation": "required", "limit": 1, "typo": 1},
])
def test_validation(entry):
    with pytest.raises(crit.CriteriaError):
        C(entry)


def test_facts_from_listing_and_merge():
    l = Listing(source="x", source_id="1", url="", title="", rent_gross=None, rent_net=2000,
                available_from="immediately", features=["dishwasher"])
    f = from_listing(l, today=date(2026, 11, 1))
    assert (f["price"], f["move_in_date"], f["has_dishwasher"], f["has_washingmachine"]) == \
        (2000, date(2026, 11, 1), True, None)
    m = merge(f, {"has_washingmachine": False, "free_requirement": {"a": True}}, {"free_requirement": {"b": False}})
    assert m["has_washingmachine"] is False and m["free_requirement"] == {"a": True, "b": False}


def test_flatfox_pin_params_send_integer_prices():
    from flatfind.sources.flatfox import _pin_params
    q = crit.search_query(C(
        {"variable": "price", "aggregation": "required", "type": "tolerance", "range": [1000.5, 3999.5]},
        {"variable": "rooms", "aggregation": "required", "type": "min", "limit": 2.5},
    ))
    p = _pin_params(q)
    assert (p["min_price"], p["max_price"], p["min_rooms"]) == (1000, 4000, 2.5)
    assert type(p["min_price"]) is int and type(p["max_price"]) is int
