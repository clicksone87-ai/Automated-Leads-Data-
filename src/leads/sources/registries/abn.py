"""Australian Business Register: weekly ABN bulk extract (two zips of XML files).

The extract has state and postcode only. People's own names are never used: an individual
is included only under a registered business or trading name.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Collection, Iterator
from datetime import date
from pathlib import Path
from typing import IO
from xml.etree import ElementTree

from leads.pipeline.age import parse_date_loose
from leads.sources.base import RawRecord
from leads.sources.registries.download import download, get_text

PACKAGE_URL = "https://data.gov.au/data/api/3/action/package_show?id=abn-bulk-extract"


def bulk_urls(package_json: str) -> list[str]:
    resources = json.loads(package_json)["result"]["resources"]
    urls = sorted(r["url"] for r in resources if r["url"].lower().endswith(".zip"))
    if not urls:
        raise RuntimeError("ABN bulk extract files not found on data.gov.au")
    return urls


class AbnSource:
    name = "abn"
    country_iso2 = "AU"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir / "registries" / "abn"

    def archives(self) -> list[Path]:
        return [
            # The extract is refreshed weekly under the same name.
            download(url, self.data_dir / url.rsplit("/", 1)[-1])
            for url in bulk_urls(get_text(PACKAGE_URL))
        ]

    def parse(self, stream: IO[bytes]) -> Iterator[RawRecord]:
        root: ElementTree.Element | None = None
        for event, node in ElementTree.iterparse(stream, events=("start", "end")):
            if root is None:
                root = node
            if event == "end" and node.tag == "ABR":
                yield from self.to_records(node)
                root.clear()  # keep memory flat: files hold a million records

    def to_records(self, node: ElementTree.Element) -> Iterator[RawRecord]:
        abn = node.find("ABN")
        if abn is None or not abn.text or abn.get("status") != "ACT":
            return
        address = node.find(".//BusinessAddress/AddressDetails")
        names = [
            n.text.strip()
            for path in ("MainEntity/NonIndividualName", "OtherEntity/NonIndividualName")
            for n in node.findall(f"{path}/NonIndividualNameText")
            if n.text and n.text.strip()
        ]
        for i, name in enumerate(dict.fromkeys(names)):
            yield RawRecord(
                source=self.name,
                source_record_id=f"{abn.text}:{i}" if i else abn.text,
                name=name,
                country_iso2=self.country_iso2,
                region=address.findtext("State") if address is not None else None,
                postcode=address.findtext("Postcode") if address is not None else None,
                created_date=parse_date_loose(abn.get("ABNStatusFromDate")),
                raw={"abn": abn.text},
            )

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield active businesses of any age: an old date proves a business is too old."""
        if self.country_iso2 not in countries:
            return
        for archive in self.archives():
            with zipfile.ZipFile(archive) as zf:
                for member in sorted(n for n in zf.namelist() if n.lower().endswith(".xml")):
                    with zf.open(member) as stream:
                        yield from self.parse(stream)
