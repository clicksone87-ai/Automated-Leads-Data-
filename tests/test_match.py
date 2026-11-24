from __future__ import annotations

import pytest

from leads.pipeline.match import (
    MatchRecord,
    cluster_records,
    distinctive_names,
    haversine_m,
    lead_id_for_cluster,
    make_lead_id,
)


def rec(src: str, rid: str, name: str, **kw: object) -> MatchRecord:
    return MatchRecord(
        source=src, source_record_id=rid, name_norm=name, country_iso2=kw.pop("country", "GB"), **kw
    )  # type: ignore[arg-type]


def test_domain_match() -> None:
    records = [
        rec("overture", "o1", "rosies bakery", domain="rosiesbakery.co.uk"),
        rec("foursquare", "f1", "rosie s bakery cafe", domain="rosiesbakery.co.uk"),
        rec("overture", "o2", "other place", domain="other.co.uk"),
    ]
    assert cluster_records(records) == [[0, 1], [2]]


def test_same_domain_different_country_not_merged() -> None:
    records = [
        rec("overture", "o1", "acme", domain="acme.com", country="GB"),
        rec("overture", "o2", "acme", domain="acme.com", country="FR"),
    ]
    assert cluster_records(records) == [[0], [1]]


def test_phone_match() -> None:
    records = [
        rec("overture", "o1", "joes plumbing", phone_e164="+447400123456"),
        rec("foursquare", "f1", "joe s plumbing and heating", phone_e164="+447400123456"),
    ]
    assert cluster_records(records) == [[0, 1]]


@pytest.mark.parametrize(
    "key", [{"domain": "grandhotel.mt"}, {"phone_e164": "+35621234567"}], ids=["domain", "phone"]
)
def test_shared_key_needs_alike_names(key: dict[str, str]) -> None:
    records = [
        rec("overture", "o1", "grand hotel", **key),
        rec("overture", "o2", "mokka lobby bar", **key),
        rec("foursquare", "f1", "grand hotel malta", **key),
    ]
    assert cluster_records(records) == [[0, 2], [1]]


def test_word_spacing_does_not_split() -> None:
    records = [
        rec("overture", "o1", "pizzadog", domain="pizzadog.ie"),
        rec("overture", "o2", "pizza dog", domain="pizzadog.ie"),
    ]
    assert cluster_records(records) == [[0, 1]]


def test_common_words_do_not_make_names_alike() -> None:
    names = [f"holiday home {w}" for w in "abcdefghijklmnopqrstuvwxyz"]
    assert distinctive_names(names)[:2] == ["a", "b"]
    assert distinctive_names(["holiday home"] * 30)[0] == "holiday home"


def test_name_postcode_match() -> None:
    records = [
        rec("companies_house", "c1", "green room", postcode_key="LS14AP"),
        rec("overture", "o1", "green room", postcode_key="LS14AP"),
        rec("overture", "o2", "green room", postcode_key="M11AA"),
    ]
    assert cluster_records(records) == [[0, 1], [2]]


def test_shared_id_match_transitive() -> None:
    records = [
        rec("companies_house", "c1", "acme", shared_ids=("companies_house:123",)),
        rec(
            "foursquare",
            "f1",
            "acme widgets",
            shared_ids=("companies_house:123",),
            phone_e164="+441212345678",
        ),
        rec("overture", "o1", "acme w", phone_e164="+441212345678"),
    ]
    assert cluster_records(records) == [[0, 1, 2]]


def test_name_within_50m() -> None:
    # ~20 m apart, near-identical names
    records = [
        rec("overture", "o1", "rosies bakery", lat=53.80000, lon=-1.55000),
        rec("osm", "node/1", "rosies bakery leeds", lat=53.80015, lon=-1.55010),
    ]
    assert haversine_m(53.8, -1.55, 53.80015, -1.5501) < 50
    assert cluster_records(records, name_threshold=80) == [[0, 1]]


def test_same_name_far_apart_not_merged() -> None:
    records = [
        rec("overture", "o1", "costa coffee", lat=53.8000, lon=-1.5500),
        rec("overture", "o2", "costa coffee", lat=53.8020, lon=-1.5500),  # ~220 m
    ]
    assert cluster_records(records) == [[0], [1]]


def test_close_but_different_names_not_merged() -> None:
    records = [
        rec("overture", "o1", "rosies bakery", lat=53.8, lon=-1.55),
        rec("overture", "o2", "kebab palace", lat=53.8001, lon=-1.55),
    ]
    assert cluster_records(records) == [[0], [1]]


def test_chain_domain_not_used_as_key() -> None:
    records = [rec("overture", f"o{i}", f"branch {i}", domain="bigchain.com") for i in range(10)]
    assert len(cluster_records(records, max_shared_key_group=8)) == 10


def test_lead_id_is_stable_and_prefers_registry() -> None:
    records = [
        rec("overture", "o1", "acme"),
        rec("companies_house", "123", "acme"),
    ]
    assert lead_id_for_cluster(records) == make_lead_id("companies_house", "123")
    assert len(make_lead_id("overture", "o1")) == 20
    assert make_lead_id("overture", "o1") == make_lead_id("overture", "o1")
