"""Google Sheet lead view: full sorted lead list, filter views, attribution footer."""

from __future__ import annotations

from typing import Any

from leads.export import ATTRIBUTION, EXPORT_COLUMNS

LEADS_TAB = "Leads"

# Filter views created on the Leads tab: (title, column, values to show).
FILTER_VIEWS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("No website", "website_status", ("none", "redirect_social")),
    ("Broken or parked website", "website_status", ("broken", "parked")),
    ("Working website", "website_status", ("working",)),
    ("Has WhatsApp", "whatsapp_number", ()),  # () = non-empty
)


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def filter_view_request(
    sheet_id: int, title: str, column: str, values: tuple[str, ...], n_rows: int
) -> dict[str, Any]:
    """batchUpdate request body for one filter view on the Leads tab."""
    col = EXPORT_COLUMNS.index(column)
    letter = column_letter(col)
    condition: dict[str, Any] = (
        {"type": "NOT_BLANK"}
        if not values
        else {
            "type": "CUSTOM_FORMULA",
            "values": [
                {"userEnteredValue": "=OR(" + ",".join(f'${letter}2="{v}"' for v in values) + ")"}
            ],
        }
    )
    return {
        "addFilterView": {
            "filter": {
                "title": title,
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": n_rows + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(EXPORT_COLUMNS),
                },
                "filterSpecs": [{"columnIndex": col, "filterCriteria": {"condition": condition}}],
            }
        }
    }


def update_sheet(sheet_id: str | None = None) -> dict[str, int]:
    """Rewrite the Leads tab of ``settings.google_sheet_id``.

    TODO(build step 7):
      1. Query eligible leads like ``csv_export.export_leads`` (same filter + sort).
      2. ``values.clear`` the Leads tab, then ``values.update`` header + rows in chunks
         of ~10k rows (valueInputOption=RAW).
      3. Two rows below the data, write the attribution footer: ``ATTRIBUTION``.
      4. Delete existing filter views, add ``FILTER_VIEWS`` via ``filter_view_request``,
         freeze the header row, set a basic filter on the header.
      5. Return {"rows": n}.
    """
    _ = ATTRIBUTION
    raise NotImplementedError("sheets.update_sheet (build step 7)")
