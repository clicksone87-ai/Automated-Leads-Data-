"""Exports: Google Sheet lead view and CSV files."""

ATTRIBUTION_LINES: tuple[str, ...] = (
    "Contains data from Overture Maps Foundation (overturemaps.org), "
    "used under CDLA Permissive 2.0 / ODbL as applicable.",
    "Contains data from Foursquare Open Source Places (opensource.foursquare.com), Apache 2.0.",
    "© OpenStreetMap contributors (openstreetmap.org/copyright), ODbL.",
)
ATTRIBUTION = " ".join(ATTRIBUTION_LINES)

# Column order shared by the Sheet and CSV export.
EXPORT_COLUMNS: tuple[str, ...] = (
    "lead_id",
    "name",
    "category_group",
    "category",
    "country_iso2",
    "city",
    "region",
    "postcode",
    "address",
    "phone_e164",
    "phone_type",
    "email",
    "website",
    "website_status",
    "whatsapp_number",
    "whatsapp_evidence",
    "created_date",
    "age_source",
    "age_unknown",
    "status",
    "sources",
    "first_seen",
    "last_seen",
)
