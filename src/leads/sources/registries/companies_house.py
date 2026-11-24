"""UK Companies House.

Bulk: "Free Company Data Product" (BasicCompanyDataAsOneFile-YYYY-MM-01.zip, monthly CSV)
from https://download.companieshouse.gov.uk/en_output.html
API (needs COMPANIES_HOUSE_API_KEY): https://developer.company-information.service.gov.uk/"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date
from pathlib import Path

from leads.sources.base import RawRecord


class CompaniesHouseSource:
    name = "companies_house"
    country_iso2 = "GB"

    def __init__(self, api_key: str | None, data_dir: Path) -> None:
        self.api_key = api_key
        self.data_dir = data_dir / "registries" / "companies_house"

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active companies registered on/after ``since``.

        TODO(build step 3):
          1. Download + unzip the monthly bulk CSV into data/registries/companies_house/.
          2. DuckDB read_csv: CompanyName, CompanyNumber, CompanyStatus, IncorporationDate
             (DD/MM/YYYY), RegAddress.*, SICCode.SicText_1.
          3. Keep CompanyStatus = 'Active' and IncorporationDate >= since.
          4. category = SIC text; shared_ids = [f"companies_house:{CompanyNumber}"].
          5. Use the API only for small lookups (rate limit 600 requests / 5 min).
        """
        if self.country_iso2 not in countries:
            return iter(())
        raise NotImplementedError("companies_house.fetch (build step 3)")
