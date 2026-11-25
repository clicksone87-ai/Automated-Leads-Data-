from __future__ import annotations

import polars as pl
import pytest

from leads.pipeline.normalise import (
    is_non_business,
    is_platform,
    is_social_domain,
    map_category,
    normalise_country,
    normalise_domain,
    normalise_frame,
    normalise_name,
    normalise_phone,
    normalise_postcode,
    normalise_website,
)
from leads.settings import load_categories, load_countries, load_filters


class TestPhone:
    def test_gb_mobile_national_format(self) -> None:
        phone = normalise_phone("07400 123456", "GB")
        assert phone is not None
        assert phone.e164 == "+447400123456"
        assert phone.type == "mobile"

    def test_gb_landline_with_trunk_zero_in_brackets(self) -> None:
        phone = normalise_phone("+44 (0)121 234 5678", None)
        assert phone is not None
        assert phone.e164 == "+441212345678"
        assert phone.type == "landline"

    def test_international_00_prefix(self) -> None:
        phone = normalise_phone("0033 1 23 45 67 89", "GB")
        assert phone is not None
        assert phone.e164 == "+33123456789"

    def test_french_mobile(self) -> None:
        phone = normalise_phone("06 12 34 56 78", "FR")
        assert phone is not None
        assert (phone.e164, phone.type) == ("+33612345678", "mobile")

    def test_us_number_is_landline_or_mobile(self) -> None:
        phone = normalise_phone("(201) 555-0123", "US")
        assert phone is not None
        assert phone.e164 == "+12015550123"
        assert phone.type == "landline_or_mobile"

    @pytest.mark.parametrize("raw", [None, "", "   ", "abc", "123", "+44 1"])
    def test_invalid(self, raw: str | None) -> None:
        assert normalise_phone(raw, "GB") is None


class TestDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://www.Example.co.uk/about?x=1", "example.co.uk"),
            ("example.com", "example.com"),
            ("http://shop.example.com:8080/", "example.com"),
            ("WWW.EXAMPLE.DE.", "example.de"),
            ("https://joes-plumbing.wixsite.com/home", "joes-plumbing.wixsite.com"),
            ("https://someone.github.io/", "someone.github.io"),
            ("https://www.facebook.com/rosies", "facebook.com"),
        ],
    )
    def test_normalise_domain(self, raw: str, expected: str) -> None:
        assert normalise_domain(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "localhost", "http://192.168.0.1/", "not a url"])
    def test_invalid_domain(self, raw: str | None) -> None:
        assert normalise_domain(raw) is None

    def test_social(self) -> None:
        assert is_social_domain(normalise_domain("https://m.facebook.com/x"))
        assert not is_social_domain(normalise_domain("https://rosiesbakery.co.uk"))

    def test_website_adds_scheme(self) -> None:
        assert normalise_website("rosiesbakery.co.uk") == "https://rosiesbakery.co.uk"
        assert normalise_website("http://a.com/x") == "http://a.com/x"
        assert normalise_website("mailto:a@b.com") is None
        assert normalise_website("  ") is None


class TestName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Rosie's Bakery Ltd.", "rosie s bakery"),
            ("Café de l'Étoile S.A.R.L.", "cafe de l etoile"),
            ("Müller & Söhne GmbH", "muller and sohne"),
            ("ACME Holdings Pty Ltd", "acme holdings"),
            ("Kowalski Sp. z o.o.", "kowalski"),
            ("The Green Room", "green room"),
            ("ООО Ромашка", "ромашка"),
            ("Luxe Spa", "luxe spa"),
            ("Ltd", "ltd"),  # never reduce a name to nothing
            ("", ""),
        ],
    )
    def test_normalise_name(self, raw: str, expected: str) -> None:
        assert normalise_name(raw) == expected


class TestCountryAndPostcode:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("gb", "GB"), (" UK ", "GB"), ("EL", "GR"), ("FRA", None), ("", None), (None, None)],
    )
    def test_country(self, raw: str | None, expected: str | None) -> None:
        assert normalise_country(raw) == expected

    def test_postcode(self) -> None:
        assert normalise_postcode("sw1a 1aa") == "SW1A1AA"
        assert normalise_postcode("75008") == "75008"
        assert normalise_postcode("1234-567") == "1234567"
        assert normalise_postcode(None) is None


class TestCountriesConfig:
    def test_included_and_excluded(self) -> None:
        countries = load_countries()
        for code in ("GB", "US", "RU", "TR", "KZ", "AE", "BR", "ZA", "AU", "XK"):
            assert code in countries
        for code in ("IR", "IL", "IN", "CN", "JP", "PK", "TH"):
            assert code not in countries


class TestCategory:
    def test_exact_keyword_default(self) -> None:
        cfg = load_categories()
        assert map_category("overture", "Hair_Salon", cfg) == "beauty_wellness"
        assert map_category("foursquare", "Dining and Drinking > Pizzeria", cfg) == "food_drink"
        assert map_category("osm", "something odd", cfg) == "other"
        assert map_category("osm", None, cfg) == "other"


def test_normalise_frame_filters_country_and_empty_names() -> None:
    raw = pl.DataFrame(
        [
            {
                "source": "overture",
                "source_record_id": "1",
                "name": "Rosie's Bakery Ltd",
                "country_iso2": "gb",
                "category": "bakery",
                "phone": "07400 123456",
                "website": "www.rosiesbakery.co.uk",
                "postcode": "ls1 4ap",
                "lat": 53.8,
                "lon": -1.5,
            },
            {
                "source": "overture",
                "source_record_id": "2",
                "name": "Tokyo Ramen",
                "country_iso2": "JP",
                "category": "restaurant",
            },
            {
                "source": "overture",
                "source_record_id": "3",
                "name": "!!!",
                "country_iso2": "GB",
                "category": None,
            },
        ]
    )
    out = normalise_frame(raw, load_categories(), load_countries())
    assert out.height == 1
    row = out.row(0, named=True)
    assert row["country_iso2"] == "GB"
    assert row["phone_e164"] == "+447400123456"
    assert row["domain"] == "rosiesbakery.co.uk"
    assert row["postcode_key"] == "LS14AP"
    assert row["category_group"] == "food_drink"


class TestFilters:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://linktr.ee/blue", True),
            ("https://www.tripadvisor.ie/x", True),
            ("https://wa.me/35699999999", True),
            ("https://maps.google.com/?cid=1", True),
            ("https://sites.google.com/view/blue", False),
            ("https://blue.example.com", False),
            (None, False),
        ],
    )
    def test_is_platform(self, url: str | None, expected: bool) -> None:
        assert is_platform(url, load_filters()) is expected

    @pytest.mark.parametrize(
        ("source", "category", "path", "expected"),
        [
            ("overture", "fort", ["cultural_and_historic", "historic_site", "fort"], True),
            ("overture", "historic_site", [], True),
            ("overture", "water_park", ["sports_and_recreation", "park", "water_park"], False),
            ("overture", "park", ["sports_and_recreation", "park"], True),
            ("overture", None, [], False),
            ("foursquare", "Community and Government > Spiritual Center > Church", [], True),
            ("foursquare", "Landmarks and Outdoors > Beach | Retail > Shop", [], False),
            ("companies_house", "historic_site", [], False),
        ],
    )
    def test_is_non_business(
        self, source: str, category: str | None, path: list[str], expected: bool
    ) -> None:
        assert is_non_business(source, category, path, load_filters()) is expected


@pytest.mark.parametrize(
    ("source", "category", "group"),
    [
        ("foursquare", "Business and Professional Services > Barbershop", "beauty_wellness"),
        ("overture", "health_care", "health_medical"),
        ("overture", "travel_and_transportation", "other"),
        ("overture", "nail_bar", "beauty_wellness"),
        ("overture", "bed_and_breakfast", "hospitality"),
        ("overture", "sports_bar", "food_drink"),
        ("overture", "car_wash", "automotive"),
        ("overture", "cocktail_bars", "food_drink"),
    ],
)
def test_category_words(source: str, category: str, group: str) -> None:
    assert map_category(source, category, load_categories()) == group
