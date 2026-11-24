"""France SIRENE (INSEE).

Bulk: "Base Sirene des entreprises et de leurs établissements" stock files (parquet/CSV)
on https://www.data.gouv.fr/ (StockUniteLegale, StockEtablissement).
API: https://portail-api.insee.fr/ (needs INSEE_API_KEY) for incremental updates."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date
from pathlib import Path

from leads.sources.base import RawRecord


class SireneSource:
    name = "sirene"
    country_iso2 = "FR"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "sirene"

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active companies registered on/after ``since``.

        TODO(build step 3):
          1. Download StockEtablissement (+ geolocated variant for lat/lon) parquet.
          2. Keep etatAdministratifEtablissement = 'A' and
             dateCreationEtablissement >= since.
          3. Name: enseigne1Etablissement or denominationUniteLegale; NAF code -> category.
          4. shared_ids = [f"sirene:{siret}"]; created_date = dateCreationEtablissement.
        """
        if self.country_iso2 not in countries:
            return iter(())
        raise NotImplementedError("sirene.fetch (build step 3)")
