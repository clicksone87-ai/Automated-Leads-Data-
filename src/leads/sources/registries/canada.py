"""Corporations Canada (federal corporations).

Open data: https://open.canada.ca/ dataset "Federal corporations" (OPEN_DATA_*.zip XML).
Provincial registries (Ontario, Quebec REQ, BC) are separate — add later."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date
from pathlib import Path

from leads.sources.base import RawRecord


class CanadaSource:
    name = "canada"
    country_iso2 = "CA"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "canada"

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active companies registered on/after ``since``.

        TODO(build step 3):
          1. Download + parse the federal corporations XML.
          2. Keep status Active with incorporation/amalgamation date >= since.
          3. shared_ids = [f"canada:{corporation_id}"].
        """
        if self.country_iso2 not in countries:
            return iter(())
        raise NotImplementedError("canada.fetch (build step 3)")
