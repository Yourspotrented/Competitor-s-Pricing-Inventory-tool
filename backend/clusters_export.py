"""
Fill the team's Clusters.xlsx (Cluster, Facility Id, Name, Our total inv
left, Competitor total Inv left, Time period) with the latest scan data,
matched by Facility Id (column B) — same file/columns the team originally
asked to have "Competitor total Inv left" filled into.

Only D ("Our total inv left") and E ("Competitor total Inv left") are
written; everything else in the sheet (Cluster names, facility names, the
Time period note) is left exactly as the team has it, since those aren't
ours to touch.

Usage:
    from clusters_export import build_filled_clusters_workbook
    xlsx_bytes = build_filled_clusters_workbook(facility_rows)
    # facility_rows: the list from GET /api/facility-competitors
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any, Dict, List, Optional

import openpyxl

logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "Clusters.xlsx")

_COL_FACILITY_ID = "B"
_COL_OUR_INV = "D"
_COL_COMPETITOR_INV = "E"


def build_filled_clusters_workbook(facility_rows: List[Dict[str, Any]],
                                    template_path: Optional[str] = None) -> bytes:
    """
    Load the Clusters.xlsx template, fill in "Our total inv left" (from
    Notion's Inventory field, if present) and "Competitor total Inv left"
    (from the latest facility-competitor scan) for every matching Facility
    Id, and return the resulting workbook as bytes ready to upload.

    facility_rows is expected in the shape returned by
    GET /api/facility-competitors — needs facility_id,
    competitor_total_inv_left, and our_inventory per row.
    """
    path = template_path or _DEFAULT_TEMPLATE_PATH
    wb = openpyxl.load_workbook(path)
    ws = wb.active

    # A handful of rows in the template have merged cells spanning columns
    # that include D/E (likely stray formatting, not data) — writing to a
    # merged cell that isn't the range's top-left anchor raises
    # AttributeError, so any merge touching a column we write to is undone
    # first. Only unmerges the specific ranges in the way, not the whole sheet.
    _COL_INDEXES = {openpyxl.utils.column_index_from_string(c) for c in (_COL_OUR_INV, _COL_COMPETITOR_INV)}
    for merged_range in list(ws.merged_cells.ranges):
        if any(col in _COL_INDEXES for col in range(merged_range.min_col, merged_range.max_col + 1)):
            ws.unmerge_cells(str(merged_range))

    by_facility_id = {str(r["facility_id"]): r for r in facility_rows if r.get("facility_id")}

    filled = 0
    for row_idx in range(2, ws.max_row + 1):
        facility_id = ws[f"{_COL_FACILITY_ID}{row_idx}"].value
        if facility_id is None:
            continue
        r = by_facility_id.get(str(facility_id))
        if r is None:
            continue

        if r.get("our_inventory") is not None:
            ws[f"{_COL_OUR_INV}{row_idx}"] = r["our_inventory"]
        if r.get("competitor_total_inv_left") is not None:
            ws[f"{_COL_COMPETITOR_INV}{row_idx}"] = r["competitor_total_inv_left"]
        filled += 1

    logger.info("Filled %d rows in Clusters.xlsx export (of %d facility rows available)",
                filled, len(facility_rows))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
