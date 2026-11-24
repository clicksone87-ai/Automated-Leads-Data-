from __future__ import annotations

from datetime import date, datetime

import pytest

from leads.pipeline.age import (
    AgeEvidence,
    eligible_for_outreach,
    keep_lead,
    parse_date_loose,
    resolve_age,
    years_in_business_to_date,
)

TODAY = date(2026, 11, 24)
SINCE = date(2018, 1, 1)


class TestResolveAge:
    def test_earliest_real_evidence_wins(self) -> None:
        result = resolve_age(
            AgeEvidence(
                registry=date(2020, 5, 1),
                fsq_date_created=date(2019, 3, 2),
                wayback_first_capture=date(2021, 1, 1),
                first_seen=date(2026, 1, 1),
            ),
            today=TODAY,
        )
        assert result.created_date == date(2019, 3, 2)
        assert result.age_source == "foursquare"
        assert result.age_unknown is False

    def test_old_registry_date_makes_lead_old(self) -> None:
        result = resolve_age(
            AgeEvidence(registry=date(2009, 6, 1), fsq_date_created=date(2021, 1, 1)), today=TODAY
        )
        assert result.created_date == date(2009, 6, 1)
        assert result.age_source == "registry"

    def test_years_in_business(self) -> None:
        result = resolve_age(AgeEvidence(years_in_business=5), today=TODAY)
        assert result.created_date == date(2021, 1, 1)
        assert result.age_source == "years_in_business"

    def test_only_first_seen_is_unknown_age(self) -> None:
        result = resolve_age(AgeEvidence(first_seen=date(2026, 2, 1)), today=TODAY)
        assert result.created_date == date(2026, 2, 1)
        assert result.age_source == "first_seen"
        assert result.age_unknown is True

    def test_no_evidence(self) -> None:
        result = resolve_age(AgeEvidence(), today=TODAY)
        assert (result.created_date, result.age_source, result.age_unknown) == (None, None, True)

    def test_future_dates_ignored(self) -> None:
        result = resolve_age(AgeEvidence(registry=date(2030, 1, 1)), today=TODAY)
        assert result.age_unknown is True


@pytest.mark.parametrize(
    ("years", "expected"), [(0, date(2026, 1, 1)), (-1, None), (301, None), (None, None)]
)
def test_years_in_business_to_date(years: int | None, expected: date | None) -> None:
    assert years_in_business_to_date(years, TODAY) == expected


class TestKeepLead:
    def test_new_business_kept(self) -> None:
        assert keep_lead("active", date(2018, 1, 1), False, SINCE)
        assert keep_lead("active", date(2024, 7, 1), False, SINCE)

    def test_old_business_dropped(self) -> None:
        assert not keep_lead("active", date(2017, 12, 31), False, SINCE)

    def test_closed_dropped(self) -> None:
        assert not keep_lead("closed", date(2024, 1, 1), False, SINCE)
        assert not keep_lead("closed", None, True, SINCE)

    def test_unknown_age_kept(self) -> None:
        assert keep_lead("active", None, True, SINCE)

    def test_outreach_respects_include_unknown_age(self) -> None:
        assert eligible_for_outreach("active", None, True, SINCE, include_unknown_age=True)
        assert not eligible_for_outreach("active", None, True, SINCE, include_unknown_age=False)
        assert eligible_for_outreach(
            "active", date(2022, 1, 1), False, SINCE, include_unknown_age=False
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2019-05-02", date(2019, 5, 2)),
        ("20190502", date(2019, 5, 2)),
        ("2019-05-02T10:11:12", date(2019, 5, 2)),
        ("02/05/2019", date(2019, 5, 2)),
        ("2019", date(2019, 1, 1)),
        (datetime(2020, 1, 2, 3, 4), date(2020, 1, 2)),
        (date(2021, 1, 1), date(2021, 1, 1)),
        ("garbage", None),
        (None, None),
    ],
)
def test_parse_date_loose(value: object, expected: date | None) -> None:
    assert parse_date_loose(value) == expected  # type: ignore[arg-type]
