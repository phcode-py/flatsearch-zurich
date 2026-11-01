"""Edge cases of scoring and ranking."""

from datetime import date

import pytest

from flatfind import criteria as crit
from flatfind.models import Listing
from flatfind.store import Store


def C(*entries, unknown_score=0.5):
    return crit.Criteria([crit._parse(i, e) for i, e in enumerate(entries)], unknown_score)


REQ_PRICE = {"variable": "price", "aggregation": "required", "type": "max", "limit": 4000}
REQ_ROOMS = {"variable": "rooms", "aggregation": "required", "type": "min", "limit": 2.5}
W_PRICE = {"variable": "price", "aggregation": "weighted", "weight": 1, "type": "max", "ideal": 2500, "limit": 4000}
W_DISH = {"variable": "has_dishwasher", "aggregation": "weighted", "weight": 1, "value": True}


# ---- boundaries ----------------------------------------------------------------


@pytest.mark.parametrize("price,ok", [(3999.99, True), (4000, True), (4000.01, False)])
def test_required_max_limit_is_inclusive(price, ok):
    assert crit.evaluate(C(REQ_PRICE), {"price": price}).passed is ok


@pytest.mark.parametrize("rooms,ok", [(2, False), (2.5, True), (3, True)])
def test_required_min_limit_is_inclusive(rooms, ok):
    assert crit.evaluate(C(REQ_ROOMS), {"rooms": rooms}).passed is ok


@pytest.mark.parametrize("rooms,ok", [(2.49, False), (2.5, True), (4.5, True), (4.51, False)])
def test_required_tolerance_edges_are_inclusive(rooms, ok):
    c = C({"variable": "rooms", "aggregation": "required", "type": "tolerance", "range": [2.5, 4.5]})
    assert crit.evaluate(c, {"rooms": rooms}).passed is ok


def test_weighted_rewards_are_clamped_and_exact_at_anchors():
    c = C(W_PRICE)
    assert [crit.evaluate(c, {"price": p}).score for p in (0, 2500, 3250, 4000, 99_999)] == [1, 1, 0.5, 0, 0]


def test_date_tolerance_on_iso_strings_and_dates_agree():
    c = C({"variable": "move_in_date", "aggregation": "weighted", "weight": 1, "type": "tolerance",
           "range": ["2027-01-01", "2027-01-31"]})
    assert crit.evaluate(c, {"move_in_date": "2027-01-16"}).score == crit.evaluate(c, {"move_in_date": date(2027, 1, 16)}).score == 1


# ---- aggregation ---------------------------------------------------------------


def test_required_failure_zeroes_a_perfect_listing():
    ev = crit.evaluate(C(REQ_PRICE, W_DISH), {"price": 4001, "has_dishwasher": True})
    assert (ev.passed, ev.score) == (False, 0)


def test_one_failed_required_among_many_is_enough():
    ev = crit.evaluate(C(REQ_PRICE, REQ_ROOMS), {"price": 1000, "rooms": 1})
    assert not ev.passed and len(ev.failures) == 1 and ev.failures[0].startswith("rooms")


def test_only_required_criteria_scores_one():
    assert crit.evaluate(C(REQ_PRICE), {"price": 3000}).score == 1


def test_weights_are_relative():
    a = C({**W_PRICE, "weight": 1}, {**W_DISH, "weight": 3})
    b = C({**W_PRICE, "weight": 10}, {**W_DISH, "weight": 30})
    facts = {"price": 4000, "has_dishwasher": True}
    assert crit.evaluate(a, facts).score == crit.evaluate(b, facts).score == 0.75


def test_same_variable_twice_counts_twice():
    c = C(W_PRICE, {**W_PRICE, "ideal": 3000, "limit": 3500})
    assert crit.evaluate(c, {"price": 3250}).score == pytest.approx((0.5 + 0.5) / 2)


def test_boolean_false_preference():
    c = C({"variable": "is_dachgeschoss", "aggregation": "weighted", "weight": 1, "value": False})
    assert crit.evaluate(c, {"is_dachgeschoss": False}).score == 1
    assert crit.evaluate(c, {"is_dachgeschoss": True}).score == 0


# ---- unknowns ------------------------------------------------------------------


@pytest.mark.parametrize("u", [0.0, 0.5, 1.0])
def test_all_unknown_scores_unknown_score(u):
    c = C(REQ_PRICE, W_PRICE, W_DISH, unknown_score=u)
    ev = crit.evaluate(c, {})
    assert ev.passed and ev.score == u
    assert ev.unknowns == ["price", "price", "has_dishwasher"]


def test_unknown_never_beats_a_known_perfect_value():
    c = C(W_DISH)
    assert crit.evaluate(c, {"has_dishwasher": True}).score > crit.evaluate(c, {}).score


def test_unknown_score_1_lets_missing_data_tie_with_perfect():
    c = C(W_DISH, unknown_score=1.0)
    assert crit.evaluate(c, {}).score == crit.evaluate(c, {"has_dishwasher": True}).score


def test_partial_station_knowledge():
    c = C({"variable": "max_travel_time_sbb", "aggregation": "required", "stations": ["A", "B"], "limit": 20})
    assert crit.evaluate(c, {"max_travel_time_sbb": {"A": 15}}).passed  # B unknown -> passes
    assert not crit.evaluate(c, {"max_travel_time_sbb": {"A": 15, "B": 21}}).passed


def test_n_lively_as_plain_number_override():
    c = C({"variable": "n_lively", "aggregation": "weighted", "weight": 1, "type": "min", "ideal": 40, "limit": 0})
    assert crit.evaluate(c, {"n_lively": 20}).score == 0.5
    assert crit.evaluate(c, {"n_lively": {"10.0": 20}}).score == 0.5


# ---- malformed facts (e.g. from set-fact) ---------------------------------------


@pytest.mark.parametrize("facts", [
    {"price": "cheap"},
    {"move_in_date": "by agreement"},
    {"has_dishwasher": "yes"},
    {"rooms": None},
])
def test_malformed_facts_do_not_crash_and_count_as_unknown(facts):
    c = C(REQ_PRICE, W_PRICE, W_DISH, REQ_ROOMS,
          {"variable": "move_in_date", "aggregation": "weighted", "weight": 1, "type": "max",
           "ideal": "2027-01-01", "limit": "2027-03-01"})
    ev = crit.evaluate(c, facts)
    assert ev.passed
    assert set(ev.unknowns) >= {k for k in facts}


@pytest.mark.parametrize("variable,raw,expected", [
    ("price", "2500", 2500),
    ("has_dishwasher", "yes", True),
    ("has_dishwasher", "nein", False),
    ("move_in_date", "2027-01-15", "2027-01-15"),
])
def test_coerce_fact(variable, raw, expected):
    assert crit.coerce_fact(variable, raw) == expected


@pytest.mark.parametrize("variable,raw", [("price", "cheap"), ("move_in_date", "soon"), ("has_dishwasher", "maybe")])
def test_coerce_fact_rejects_garbage(variable, raw):
    with pytest.raises(crit.CriteriaError):
        crit.coerce_fact(variable, raw)


# ---- ranking -------------------------------------------------------------------


def _listing(i):
    return Listing(source="t", source_id=str(i), url="", title=f"flat {i}")


def test_rank_puts_passed_zero_above_rejected(tmp_path):
    s = Store(tmp_path / "t.db")
    for i in range(3):
        s.add(_listing(i), "new")
    s.save_evaluation("t:0", {"score": 0.0, "passed": False}, "rejected")
    s.save_evaluation("t:1", {"score": 0.0, "passed": True}, "scored")
    s.save_evaluation("t:2", {"score": 0.4, "passed": True}, "scored")
    assert [l.id for l, *_ in s.ranked(10)] == ["t:2", "t:1"]
    assert [l.id for l, *_ in s.ranked(10, include_rejected=True)] == ["t:2", "t:1", "t:0"]
