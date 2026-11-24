"""Norway Brønnøysund Register Centre (Enhetsregisteret).

Bulk: https://data.brreg.no/enhetsregisteret/api/enheter/lastned (gzip JSON/CSV) and
/underenheter/lastned for establishments. Open data (NLOD), no key needed."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date
from pathlib import Path

from leads.sources.base import RawRecord


class BrregSource:
    name = "brreg"
    country_iso2 = "NO"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "brreg"

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active companies registered on/after ``since``.

        TODO(build step 3):
          1. Download enheter + underenheter bulk files.
          2. Keep not konkurs/underAvvikling/slettedato; stiftelsesdato or
             registreringsdatoEnhetsregisteret >= since.
          3. naeringskode1.beskrivelse -> category; hjemmeside -> website.
          4. shared_ids = [f"brreg:{organisasjonsnummer}"].
        """
        if self.country_iso2 not in countries:
            return iter(())
        raise NotImplementedError("brreg.fetch (build step 3)")
