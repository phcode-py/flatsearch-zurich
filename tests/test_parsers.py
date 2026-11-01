"""Offline parser tests against real captured payloads (tests/fixtures)."""

import json
from pathlib import Path

from flatfind.models import Listing
from flatfind.sources import flatfox, homegate
from flatfind.store import Store

FIX = Path(__file__).parent / "fixtures"


def test_homegate_extract_and_parse():
    result = homegate.search_result(homegate.extract_state((FIX / "homegate_search.html").read_text()))
    assert result["hasNextPage"] is True
    l = homegate.parse(result["listings"][0])
    assert l.id == "homegate:4000000001"
    assert l.url == "https://www.homegate.ch/rent/4000000001"
    assert l.category == "apartment"
    assert (l.rooms, l.living_space_m2) == (3, 75)
    assert (l.rent_gross, l.rent_net, l.rent_charges) == (3350, 3150, 200)
    assert (l.zipcode, l.city, l.street) == ("8000", "Zürich", "Musterstrasse 1")
    assert l.images == ["https://example.com/listing/4000000001/a.jpg", "https://example.com/listing/4000000001/b.jpg"]
    assert l.description == "Beispieltext für Tests.\nAusstattung:\n- Parkett\n- Balkon"
    assert "pets_allowed" in l.features


def test_homegate_state_tolerates_undefined():
    page = '<script>window.__INITIAL_STATE__ = {"a":undefined,"b":"text with undefined in it"};</script>'
    assert homegate.extract_state(page) == {"a": None, "b": "text with undefined in it"}


def test_homegate_category_mapping():
    assert homegate._category(["APARTMENT", "FLAT"]) == "apartment"
    assert homegate._category(["HOUSE", "ROW_HOUSE"]) == "house"
    assert homegate._category(["PARKING_SPACE", "UNDERGROUND_SLOT"]) == "parking"
    assert homegate._category(["ROOM", "SINGLE_ROOM"]) == "room"


def test_flatfox_parse():
    l = flatfox.parse(json.loads((FIX / "flatfox_listing.json").read_text()))
    assert l.id == "flatfox:10000001"
    assert l.url.startswith("https://flatfox.ch/") and l.url.endswith("/10000001/")
    assert l.contact_url == "https://flatfox.ch/de/listing/10000001/submit/"
    assert l.category == "apartment"
    assert (l.rooms, l.rent_gross, l.rent_net) == (4.0, 3580, 3400)
    assert l.available_from == "2026-12-01"
    assert l.lister == "Muster Immobilien AG"
    assert l.images and l.images[0].startswith("https://flatfox.ch/media/")
    assert "balcony" in l.features  # normalized from "balconygarden"
    assert l.is_temporary is False


def test_homegate_temporary_from_title():
    item = homegate.search_result(homegate.extract_state((FIX / "homegate_search.html").read_text()))["listings"][0]
    assert homegate.parse(item).is_temporary is None
    text = item["listing"]["localization"]["de"]["text"]
    for title, temporary in [("Befristet bis März: 3 Zimmer", True), ("3 Zi (befristet bis 31.03.2027)", True),
                             ("Zwischenmiete bis Juni", True), ("Unbefristeter Mietvertrag, 3 Zimmer", None)]:
        text["title"] = title
        assert homegate.parse(item).is_temporary is temporary, title


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    l = flatfox.parse(json.loads((FIX / "flatfox_listing.json").read_text()))
    assert s.add(l, "new") is True
    assert s.add(l, "new") is False
    assert s.known_ids("flatfox") == {"10000001"}
    s.set_fact(l.id, "free_requirement", True, "parquet floor")
    assert s.get_extra(l.id)["facts"] == {"free_requirement": {"parquet floor": True}}
    got, status = s.get(l.id)
    assert status == "new" and got.to_dict() == l.to_dict() and got.raw["pk"] == 10000001
    s.set_status(l.id, "evaluated")
    assert [st for _, st in s.list()] == ["evaluated"]
