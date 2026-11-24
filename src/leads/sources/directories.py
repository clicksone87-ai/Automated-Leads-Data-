"""Placeholder for licensed directory feeds (Yellow Pages style). Optional, build step 9.

Only use feeds you hold a licence for; never scrape directory sites.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date

from leads.sources.base import RawRecord


class LicensedDirectorySource:
    name = "directories"

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """TODO(build step 9): read the licensed feed files (CSV/parquet drop in
        data/directories/<provider>/), map columns to RawRecord, keep the provider's
        licence terms in raw["licence"]."""
        raise NotImplementedError("directories.fetch (build step 9)")
