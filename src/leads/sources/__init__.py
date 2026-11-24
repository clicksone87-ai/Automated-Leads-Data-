"""Data sources and the collect step (sources -> raw zone parquet)."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import date

from leads.settings import Settings
from leads.sources.base import RawRecord, Source, write_raw

__all__ = ["SOURCE_FACTORIES", "RawRecord", "Source", "build_source", "collect", "write_raw"]


def _overture(s: Settings) -> Source:
    from leads.sources.overture import OvertureSource

    return OvertureSource(
        release=s.source_option("overture", "release"),
        base_uri=s.source_option("overture", "base_uri", "s3://overturemaps-us-west-2/release"),
        bbox=s.country_bbox,
    )


def _foursquare(s: Settings) -> Source:
    from leads.sources.foursquare import FoursquareSource

    token = s.hf_token.get_secret_value() if s.hf_token else None
    return FoursquareSource(
        uri=s.source_option("foursquare", "uri"), hf_token=token, bbox=s.country_bbox
    )


def _osm(s: Settings) -> Source:
    from leads.sources.osm import OsmSource

    return OsmSource(extracts_dir=s.source_option("osm", "extracts_dir", "data/osm"))


def _companies_house(s: Settings) -> Source:
    from leads.sources.registries.companies_house import CompaniesHouseSource

    return CompaniesHouseSource(data_dir=s.data_dir)


def _sirene(s: Settings) -> Source:
    from leads.sources.registries.sirene import SireneSource

    return SireneSource(data_dir=s.data_dir)


def _brreg(s: Settings) -> Source:
    from leads.sources.registries.brreg import BrregSource

    return BrregSource(data_dir=s.data_dir)


def _abn(s: Settings) -> Source:
    from leads.sources.registries.abn import AbnSource

    return AbnSource(data_dir=s.data_dir)


def _canada(s: Settings) -> Source:
    from leads.sources.registries.canada import CanadaSource

    return CanadaSource(data_dir=s.data_dir)


def _directories(s: Settings) -> Source:
    from leads.sources.directories import LicensedDirectorySource

    return LicensedDirectorySource()


SOURCE_FACTORIES: dict[str, Callable[[Settings], Source]] = {
    "overture": _overture,
    "foursquare": _foursquare,
    "osm": _osm,
    "companies_house": _companies_house,
    "sirene": _sirene,
    "brreg": _brreg,
    "abn": _abn,
    "canada": _canada,
    "directories": _directories,
}


def build_source(name: str, settings: Settings) -> Source:
    try:
        return SOURCE_FACTORIES[name](settings)
    except KeyError:
        raise ValueError(f"Unknown source {name!r}. Known: {sorted(SOURCE_FACTORIES)}") from None


def collect(
    source_names: list[str],
    *,
    settings: Settings,
    countries: Collection[str],
    since: date,
    run_id: str,
) -> dict[str, int]:
    """Fetch each source into the raw zone. Returns rows written per source."""
    counts: dict[str, int] = {}
    for name in source_names:
        source = build_source(name, settings)
        rows, _ = write_raw(
            source.fetch(countries, since),
            source=name,
            run_id=run_id,
            data_dir=settings.data_dir,
            chunk_size=settings.batch.raw_chunk_size,
        )
        counts[name] = rows
    return counts
