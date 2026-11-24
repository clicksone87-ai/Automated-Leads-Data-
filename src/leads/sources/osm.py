"""OpenStreetMap extracts (Geofabrik .osm.pbf), read with DuckDB spatial ``ST_ReadOSM``.

Attribution: "© OpenStreetMap contributors", ODbL (see leads.export.ATTRIBUTION).
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date
from pathlib import Path

from leads.sources.base import RawRecord

# Tags that indicate a business. Category string stored as "key=value" (see categories.yaml).
BUSINESS_TAG_KEYS = ("shop", "amenity", "craft", "office", "tourism", "healthcare", "leisure")


class OsmSource:
    name = "osm"

    def __init__(self, extracts_dir: str | Path) -> None:
        self.extracts_dir = Path(extracts_dir)

    def fetch(self, countries: Collection[str], since: date) -> Iterator[RawRecord]:
        """Yield named nodes/ways with business tags from per-country extracts.

        TODO(build step 8):
          1. Download Geofabrik extracts for ``countries`` into ``extracts_dir``.
          2. DuckDB: ``INSTALL spatial; LOAD spatial;`` then
             ``SELECT id, kind, tags, lat, lon FROM ST_ReadOSM('<file>.osm.pbf')
               WHERE tags['name'] IS NOT NULL AND (<any BUSINESS_TAG_KEYS present>)``
             (ways: use the centroid of their nodes).
          3. Map tags: name, phone/contact:phone, website/contact:website, email,
             addr:street/housenumber/city/postcode, country from the extract.
          4. ``source_record_id`` = f"{kind}/{id}" (e.g. "node/123").
          5. Age: OSM first version needs history files (full-history .osh.pbf) or the
             OSM API ``/api/0.6/node/<id>/history`` for small batches -> raw["first_version"].
        """
        raise NotImplementedError("osm.fetch (build step 8)")
