"""Australian Business Register (ABN Lookup).

Bulk: "ABN Bulk Extract" XML on https://data.gov.au/ (weekly).
Web services (needs ABN_LOOKUP_GUID): https://abr.business.gov.au/Tools/WebServices"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date
from pathlib import Path

from leads.sources.base import RawRecord


class AbnSource:
    name = "abn"
    country_iso2 = "AU"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "abn"

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active companies registered on/after ``since``.

        TODO(build step 3):
          1. Download + stream-parse the bulk XML (iterparse; files are large).
          2. Keep ABN status 'ACT' with ABNStatusFromDate >= since; prefer
             business/trading names (OtherEntity / DGR) for name.
          3. State + postcode only (no street address in the extract).
          4. shared_ids = [f"abn:{ABN}"].
        """
        if self.country_iso2 not in countries:
            return iter(())
        raise NotImplementedError("abn.fetch (build step 3)")
